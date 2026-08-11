"""从 ContextPacket 和 EvidenceLedger 确定性构造三角色报告。

本模块负责从共享证据确定性生成三份 ``RoleReportArtifact``，不负责 LLM
调用、持久化、发布或应用编排。唯一内部协作入口是
``build_role_report_templates``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypeAlias

from sleepagent.product_runtime.knowledge import grounded_citation_refs
from sleepagent.product_runtime.reports.roles import REPORT_ROLE_ORDER
from sleepagent.product_runtime.schemas import (
    ContextPacket,
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireEntry,
    RadarNightSummary,
    RiskLevel,
    RoleReportArtifact,
)


_ReportRoleValue: TypeAlias = Literal["elder", "family", "doctor"]
_JsonScalar: TypeAlias = str | int | float | bool | None
_JsonValue: TypeAlias = (
    _JsonScalar | Sequence["_JsonValue"] | Mapping[str, "_JsonValue"]
)
_ReportDataQuality: TypeAlias = dict[str, _JsonValue]
_ReportStructuredSummary: TypeAlias = dict[str, _JsonValue]


# 内部构造入口

def build_role_report_templates(
    *,
    context: ContextPacket,
    ledger: EvidenceLedger,
    risk: RiskLevel,
    safety_notices: list[str],
) -> list[RoleReportArtifact]:
    """确定性构造共享事实快照上的三角色报告模板。

    Args:
        context: 提供任务信息和报告引用上下文的 ``ContextPacket``。
        ledger: 三个角色共同使用的权威 ``EvidenceLedger``。
        risk: 已校验的报告风险等级。
        safety_notices: 每份报告必须携带的安全说明。

    Returns:
        按稳定角色顺序排列的三份 ``RoleReportArtifact``。

    Side Effects:
        无；不调用模型，也不写数据库。
    """
    claim_ids = [claim.claim_id for claim in ledger.claims]
    facts = _fact_lines(ledger)
    # 三个角色必须共享同一 ledger 与事实快照，避免呈现差异演变为事实分叉。
    fact_snapshot = list(ledger.claims)
    latest_summary = (
        context.evidence_packet.night_summaries[-1]
        if context.evidence_packet.night_summaries
        else None
    )
    data_quality = _data_quality_packet(ledger, latest_summary)
    trend_highlights = _trend_highlights(ledger)
    anomaly_highlights = _anomaly_highlights(ledger)
    confirmation_actions = _confirmation_actions(risk)
    # 固定角色顺序是调用方和序列化输出的稳定合同，不能依赖集合或临时排序。
    return [
        _template_report(
            context=context,
            ledger=ledger,
            role=role.value,
            risk=risk,
            claim_ids=claim_ids,
            facts=facts,
            fact_snapshot=fact_snapshot,
            data_quality=data_quality,
            trend_highlights=trend_highlights,
            anomaly_highlights=anomaly_highlights,
            confirmation_actions=confirmation_actions,
            latest_summary=latest_summary,
            safety_notices=safety_notices,
        )
        for role in REPORT_ROLE_ORDER
    ]


def _fact_lines(ledger: EvidenceLedger) -> list[str]:
    return [claim.text for claim in ledger.claims] or ["当前没有足够证据形成健康趋势结论。"]


def _title(role: _ReportRoleValue) -> str:
    return {"elder": "昨夜睡眠观察", "family": "家属照护摘要", "doctor": "雷达观察证据摘要"}[role]


def _template_report(
    *,
    context: ContextPacket,
    ledger: EvidenceLedger,
    role: _ReportRoleValue,
    risk: RiskLevel,
    claim_ids: list[str],
    facts: list[str],
    fact_snapshot: list[EvidenceClaim],
    data_quality: _ReportDataQuality,
    trend_highlights: list[str],
    anomaly_highlights: list[str],
    confirmation_actions: list[str],
    latest_summary: RadarNightSummary | None,
    safety_notices: list[str],
) -> RoleReportArtifact:
    evidence_refs = grounded_citation_refs(context, ledger, role=role)
    caveats = _caveats(role, ledger, latest_summary)
    structured_summary = _structured_summary(
        role=role,
        ledger=ledger,
        risk=risk,
        facts=fact_snapshot,
        evidence_refs=evidence_refs,
        data_quality=data_quality,
        trend_highlights=trend_highlights,
        anomaly_highlights=anomaly_highlights,
        confirmation_actions=confirmation_actions,
        caveats=caveats,
        safety_notices=safety_notices,
    )
    return RoleReportArtifact(
        artifact_id=f"report:{context.task_context.task_id}:{role}",
        task_id=context.task_context.task_id,
        role=role,
        title=_title(role),
        content=_role_content(
            role=role,
            risk=risk,
            facts=facts,
            fact_snapshot=fact_snapshot,
            evidence_refs=evidence_refs,
            data_quality=data_quality,
            questionnaires=ledger.questionnaire_entries,
            trend_highlights=trend_highlights,
            anomaly_highlights=anomaly_highlights,
            confirmation_actions=confirmation_actions,
            caveats=caveats,
            safety_notices=safety_notices,
        ),
        source_ledger_id=ledger.ledger_id,
        risk_level=risk,
        claim_ids=claim_ids,
        facts=fact_snapshot,
        evidence_refs=evidence_refs,
        source_refs=evidence_refs,
        trend_highlights=trend_highlights,
        anomaly_highlights=anomaly_highlights,
        # confirmation actions 只面向家属照护协同，避免其他角色误解为待执行指令。
        confirmation_actions=confirmation_actions if role == "family" else [],
        data_quality=data_quality,
        questionnaire_entries=list(ledger.questionnaire_entries),
        structured_summary=structured_summary,
        caveats=caveats,
        safety_notices=list(safety_notices),
        generation_mode="template",
    )


# Evidence/质量摘要辅助函数

def _caveats(
    role: _ReportRoleValue,
    ledger: EvidenceLedger,
    latest_summary: RadarNightSummary | None,
) -> list[str]:
    claim_caveats = [caveat for claim in ledger.claims for caveat in claim.caveats]
    summary_caveats = list(latest_summary.caveats) if latest_summary else []
    # caveat 去重仍保留来源顺序，使共同边界与追加的专业边界保持可审阅语境。
    common = list(
        dict.fromkeys(
            ledger.caveats
            + claim_caveats
            + summary_caveats
            + ["仅作睡眠健康观察参考，不构成诊断。"]
        )
    )
    if role == "doctor":
        return list(
            dict.fromkeys(
                common + ["毫米波雷达不能替代 PSG、病史、查体或临床判断。"]
            )
        )
    return common


def _data_quality_packet(
    ledger: EvidenceLedger,
    summary: RadarNightSummary | None,
) -> _ReportDataQuality:
    packet: _ReportDataQuality = {
        "status": ledger.derived_metrics.get("data_quality_status", "unknown"),
        "coverage_ratio": ledger.derived_metrics.get("data_coverage_ratio"),
        "uncertainty": ledger.uncertainty,
    }
    if summary is None:
        return packet
    packet.update(
        {
            "status": summary.data_quality_status.value,
            "coverage_ratio": summary.data_coverage_ratio,
            "confidence_label": summary.confidence_label,
            "device_status": summary.device_status.value,
            "health_conclusion_allowed": summary.health_conclusion_allowed,
            "invalid_reading_count": summary.invalid_reading_count,
            "abnormal_reading_count": summary.abnormal_reading_count,
            "missing_intervals": list(summary.missing_intervals),
            "out_of_bed_intervals": list(summary.out_of_bed_intervals),
            "not_in_bed_intervals": list(summary.not_in_bed_intervals),
            "quality_reasons": list(summary.quality_reasons),
            "blocked_reasons": list(summary.blocked_reasons),
        }
    )
    return packet


def _trend_highlights(ledger: EvidenceLedger) -> list[str]:
    return [
        claim.text
        for claim in ledger.claims
        if claim.source_kind == "trend_tool"
    ]


def _anomaly_highlights(ledger: EvidenceLedger) -> list[str]:
    return [
        claim.text
        for claim in ledger.claims
        if claim.risk_level
        in {
            RiskLevel.WATCH,
            RiskLevel.ESCALATE,
            RiskLevel.URGENT_BOUNDARY,
            RiskLevel.UNCERTAIN,
        }
    ]


def _confirmation_actions(risk: RiskLevel) -> list[str]:
    return {
        RiskLevel.INFO: [],
        RiskLevel.WATCH: ["确认是否启用持续提醒", "确认是否发送补充微问卷"],
        RiskLevel.ESCALATE: ["确认导出医生材料", "确认预约评估建议卡片"],
        RiskLevel.UNCERTAIN: ["确认补充观察或问卷后再评估"],
        RiskLevel.URGENT_BOUNDARY: ["记录家属通知与送达状态"],
    }[risk]


# 角色结构化摘要与正文渲染

def _structured_summary(
    *,
    role: _ReportRoleValue,
    ledger: EvidenceLedger,
    risk: RiskLevel,
    facts: list[EvidenceClaim],
    evidence_refs: list[str],
    data_quality: _ReportDataQuality,
    trend_highlights: list[str],
    anomaly_highlights: list[str],
    confirmation_actions: list[str],
    caveats: list[str],
    safety_notices: list[str],
) -> _ReportStructuredSummary:
    common: _ReportStructuredSummary = {
        "source_ledger_id": ledger.ledger_id,
        "risk_level": risk.value,
        "claim_ids": [claim.claim_id for claim in facts],
    }
    if role == "elder":
        return {
            **common,
            "primary_observation": facts[0].text if facts else "现有数据不足。",
            "primary_suggestion": _elder_suggestion(risk),
        }
    if role == "family":
        return {
            **common,
            "trend_highlights": trend_highlights,
            "anomaly_highlights": anomaly_highlights,
            "confirmation_actions": confirmation_actions,
        }
    # 医生报告用于专业审阅，必须携带完整质量信息并显式保留非诊断边界。
    return {
        **common,
        "evidence_chain": [claim.model_dump(mode="json") for claim in facts],
        "data_quality": data_quality,
        "questionnaire_entries": [
            entry.model_dump(mode="json") for entry in ledger.questionnaire_entries
        ],
        "source_refs": evidence_refs,
        "caveats": caveats,
        "safety_notices": safety_notices,
        "non_diagnostic_boundary": True,
    }


def _role_content(
    *,
    role: _ReportRoleValue,
    risk: RiskLevel,
    facts: list[str],
    fact_snapshot: list[EvidenceClaim],
    evidence_refs: list[str],
    data_quality: _ReportDataQuality,
    questionnaires: list[QuestionnaireEntry],
    trend_highlights: list[str],
    anomaly_highlights: list[str],
    confirmation_actions: list[str],
    caveats: list[str],
    safety_notices: list[str],
) -> str:
    if role == "elder":
        lines = ["我们根据昨夜设备记录，为您整理了这些观察："]
        lines.extend(f"- {fact}" for fact in facts)
        lines.append(f"当前为 {risk.value} 级观察提示。{_elder_suggestion(risk)}")
        return "\n".join(
            [*lines, "【说明】", *[f"- {item}" for item in safety_notices]]
        )
    if role == "family":
        return "\n".join(
            [
                "以下内容用于家属连续观察与照护协同：",
                "【共同事实】",
                *[f"- {fact}" for fact in facts],
                "【趋势】",
                *(
                    [f"- {item}" for item in trend_highlights]
                    or ["- 暂无可解释的连续趋势。"]
                ),
                "【异常线索】",
                *(
                    [f"- {item}" for item in anomaly_highlights]
                    or ["- 暂无额外异常线索。"]
                ),
                "【待确认动作】",
                *(
                    [f"- {item}" for item in confirmation_actions]
                    or ["- 当前无需额外确认动作。"]
                ),
                f"风险线索级别：{risk.value}。",
                "【说明】",
                *[f"- {item}" for item in safety_notices],
            ]
        )
    evidence_lines = [
        (
            f"- {claim.claim_id}: {claim.text} | confidence={claim.confidence:.2f}"
            f" | risk={claim.risk_level.value}"
            f" | evidence={','.join(claim.evidence_refs)}"
            f" | uncertainty={claim.uncertainty or 'none'}"
            f" | caveats={';'.join(claim.caveats) or 'none'}"
        )
        for claim in fact_snapshot
    ]
    questionnaire_lines = [
        f"- {entry.question_id}: {entry.answer}（来源 {entry.question_source}:{entry.source_id or 'legacy'}）"
        for entry in questionnaires
    ]
    quality_lines = [f"- {key}: {value}" for key, value in data_quality.items()]
    return "\n".join(
        [
            "以下为毫米波雷达 canonical 数据与结构化 Evidence Ledger 摘要：",
            f"【风险状态】{risk.value}",
            "【结构化证据链】",
            *(evidence_lines or ["- 当前无可发布 claim。"]),
            "【数据质量】",
            *(quality_lines or ["- 未提供数据质量摘要。"]),
            "【补充问卷】",
            *(questionnaire_lines or ["- 无补充问卷。"]),
            "【来源】",
            *[f"- {ref}" for ref in evidence_refs],
            "【完整 caveat】",
            *[f"- {caveat}" for caveat in caveats],
            "【安全与合规说明】",
            *[f"- {item}" for item in safety_notices],
        ]
    )


def _elder_suggestion(risk: RiskLevel) -> str:
    if risk == RiskLevel.INFO:
        return "先安心休息，继续保持设备正常摆放即可。"
    if risk == RiskLevel.WATCH:
        return "建议请家人一起留意接下来几晚的变化。"
    if risk in {RiskLevel.ESCALATE, RiskLevel.UNCERTAIN}:
        return "建议让家人协助整理材料并联系医生进一步评估。"
    return "如有胸痛、严重呼吸困难或意识异常，请及时寻求线下医疗或急救评估。"


__all__ = ["build_role_report_templates"]
