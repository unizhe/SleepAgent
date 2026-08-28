"""角色材料渲染与报告合同。"""

from __future__ import annotations


# 合并自 reports/roles.py。
"""定义角色报告的稳定角色合同，不负责报告渲染。"""

from enum import Enum

from pydantic import Field, field_serializer, model_validator

from sleepagent.runtime.schemas import RadarAgentSchema, RoleReportArtifact


# 三角色枚举是调用方共享的稳定角色标识合同。
class ReportRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


REPORT_ROLE_ORDER: tuple[ReportRole, ...] = (
    ReportRole.ELDER,
    ReportRole.FAMILY,
    ReportRole.DOCTOR,
)


# 角色报告集合的稳定 Pydantic 合同；它不构造或渲染报告内容。
class RoleReportBundle(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    reports: list[RoleReportArtifact] = Field(default_factory=list)

    def for_role(self, role: ReportRole) -> RoleReportArtifact | None:
        """按角色查找报告。

        Args:
            role: 要查找的稳定报告角色。

        Returns:
            匹配角色的报告；找不到时返回 ``None``。
        """
        return next((report for report in self.reports if report.role == role.value), None)


# 合并自 reports/rendering.py。
"""从 ContextPacket 和 EvidenceLedger 确定性构造三角色报告。

本模块负责从共享证据确定性生成三份 ``RoleReportArtifact``，不负责 LLM
调用、持久化、发布或应用编排。唯一内部协作入口是
``build_role_report_templates``。
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, TypeAlias

from sleepagent.runtime.knowledge import grounded_citation_refs
from sleepagent.runtime.schemas import (
    ContextPacket,
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireEntry,
    RadarNightSummary,
    RiskLevel,
    RoleReportArtifact,
)
from sleepagent.runtime.contracts import (
    AgentEnvelope,
    AgentId,
    CareStrategy as ProductCareStrategy,
    CommunicationDraft,
    EvidencePacket as ProductEvidencePacket,
    SafetyDecision as ProductSafetyDecision,
    SafetyVerdict,
    StrictContract,
    ToolReceipt,
    WorkProductKind,
    stable_hash,
)
from sleepagent.runtime.governance import AcceptedWorkProduct
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.results import (
    PRODUCT_EPISODE_RUNNER_VERSION,
    ProductEpisodeRunRequest,
    product_episode_request_hash,
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


def build_safe_model_pin(
    *,
    implementation: str,
    provider: str,
    model_id: str,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_type: str | None = None,
    retry: int | None = None,
    timeout_seconds: float | None = None,
    provider_endpoint: str | None = None,
    deployment_mode: str | None = None,
    data_mode: str | None = None,
) -> dict[str, Any]:
    """Build a non-secret semantic model pin shared by API and worker."""

    semantic_config: dict[str, Any] = {}
    openai_config_present = any(
        value is not None
        for value in (
            temperature,
            max_output_tokens,
            retry,
            timeout_seconds,
            provider_endpoint,
        )
    )
    if temperature is not None:
        semantic_config["temperature"] = temperature
    if max_output_tokens is not None:
        semantic_config["max_output_tokens"] = max_output_tokens
    if openai_config_present:
        semantic_config["thinking_type"] = thinking_type
    if retry is not None:
        semantic_config["retry"] = retry
    if timeout_seconds is not None:
        semantic_config["timeout_seconds"] = timeout_seconds
    if provider_endpoint is not None and provider_endpoint.strip():
        semantic_config["provider_endpoint_sha256"] = stable_hash(
            provider_endpoint.rstrip("/")
        )
    if deployment_mode is not None:
        semantic_config["deployment_mode"] = deployment_mode
    if data_mode is not None:
        semantic_config["data_mode"] = data_mode
    return {
        "implementation": implementation,
        "provider": provider,
        "model_id": model_id,
        "semantic_config": semantic_config,
    }


def build_shared_analysis_runtime_manifest(
    *,
    sleepcare_control: Mapping[str, Any],
    agents: Mapping[str, Any],
    tools: Mapping[str, Any],
    care_catalog: Mapping[str, Any],
    prompt_compiler_version: str,
    safety_policy_version: str,
    runner_version: str = PRODUCT_EPISODE_RUNNER_VERSION,
    analysis_topology_version: str = "shared-analysis-topology.v1",
    personalization_projection_version: str = "selected_stable.v1",
) -> dict[str, Any]:
    """Canonical role-neutral manifest; excludes projection/render pins."""

    catalog = dict(care_catalog)
    return {
        "schema_version": "shared_analysis_runtime_manifest.v2",
        "runner_version": runner_version,
        "shared_analysis_schema": SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION,
        "prompt_compiler_version": prompt_compiler_version,
        "safety_policy_version": safety_policy_version,
        "analysis_topology_version": analysis_topology_version,
        "personalization_projection_version": (
            personalization_projection_version
        ),
        "sleepcare_control": dict(sleepcare_control),
        "agents": {
            key: dict(value)
            for key, value in sorted(agents.items())
        },
        "tools": {
            key: dict(value)
            for key, value in sorted(tools.items())
        },
        "care_catalog_sha256": stable_hash(catalog),
        "care_catalog": catalog,
    }


def build_elder_narrative_runtime_manifest(
    *,
    profile: Mapping[str, Any],
    skill: Mapping[str, Any],
    model: Mapping[str, Any],
    prompt_compiler_version: str,
    safety_policy_version: str,
    content_plan_assembly: bool,
    runner_version: str = PRODUCT_EPISODE_RUNNER_VERSION,
    personalization_projection_version: str = "selected_stable.v1",
) -> dict[str, Any]:
    """Canonical render-only manifest shared by API and worker."""

    return {
        "schema_version": "elder_narrative_runtime_manifest.v1",
        "runner_version": runner_version,
        "personalization_projection_version": (
            personalization_projection_version
        ),
        "request_schema": ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION,
        "result_schema": ELDER_NARRATIVE_SCHEMA_VERSION,
        "prompt_compiler_version": prompt_compiler_version,
        "safety_policy_version": safety_policy_version,
        "render_policy_version": "elder_narrative_render.v1",
        "profile": dict(profile),
        "skill": dict(skill),
        "model": dict(model),
        "content_plan_assembly": content_plan_assembly,
    }


SHARED_ANALYSIS_SOURCE_SCHEMA_VERSION = "shared_analysis_source.v1"
SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION = "shared_night_analysis.v1"
ROLE_PROJECTION_SCHEMA_VERSION = "role_projection.v1"
ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION = "elder_narrative_request.v1"
ELDER_NARRATIVE_SCHEMA_VERSION = "elder_narrative.v1"


class RoleProjectionState(str, Enum):
    READY = "ready"
    POLICY_BLOCKED = "policy_blocked"


class ElderNarrativeState(str, Enum):
    READY = "ready"
    FALLBACK = "fallback"
    FAILED = "failed"
    STALE = "stale"


class SharedAnalysisSourceV1(StrictContract):
    """Exact, role-neutral source identity supplied by the Product worker."""

    schema_version: Literal["shared_analysis_source.v1"] = (
        SHARED_ANALYSIS_SOURCE_SCHEMA_VERSION
    )
    night_episode_id: str = Field(..., min_length=1)
    night_episode_revision_id: str = Field(..., min_length=1)
    night_episode_revision_number: int = Field(..., ge=1)
    wake_date: date
    observation_set_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    canonical_data_version: str = Field(..., min_length=1)
    desired_analysis_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    consumed_context_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    data_sufficiency: Literal["sufficient", "partial"]
    quality_state: Literal["good", "partial"]
    risk_state: str = Field(..., min_length=1)
    health_escalation_allowed: Literal[False] = False
    quality_reason_codes: tuple[str, ...] = ()
    risk_reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    partial_caveat: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_partial_caveat(self) -> "SharedAnalysisSourceV1":
        partial = self.data_sufficiency == "partial" or self.quality_state == "partial"
        if partial != (self.partial_caveat is not None):
            raise ValueError("PARTIAL shared analysis requires exactly one caveat")
        return self


class SharedAnalysisRunRequest(StrictContract):
    """System-bound request for the one role-neutral Product analysis pass."""

    source: SharedAnalysisSourceV1
    runtime_request: ProductEpisodeRunRequest

    @property
    def episode_id(self) -> str:
        return self.runtime_request.episode_id

    @model_validator(mode="after")
    def require_role_neutral_runtime_request(self) -> "SharedAnalysisRunRequest":
        request = self.runtime_request
        if request.episode_type.value != "morning_review":
            raise ValueError("shared analysis requires MORNING_REVIEW")
        if request.fact_snapshot.binding.role != "system":
            raise ValueError("shared analysis requires a system-bound FactSnapshot")
        if request.audience_role is not None or request.doctor_material:
            raise ValueError("shared analysis cannot carry an audience")
        if request.user_text or request.user_fact_responses:
            raise ValueError("shared analysis cannot consume conversational input")
        if (
            request.fact_snapshot.canonical_data_version
            != self.source.canonical_data_version
        ):
            raise ValueError("shared analysis canonical source mismatch")
        if request.fact_snapshot.source_scope.date_end != self.source.wake_date:
            raise ValueError("shared analysis wake-date scope mismatch")
        return self


def _shared_analysis_hash_material(
    *,
    source: SharedAnalysisSourceV1,
    runner_version: str,
    registry_hash: str,
    runtime_request_sha256: str,
    fact_snapshot_hash: str,
    summary_lines: tuple[str, ...],
    evidence: AcceptedWorkProduct,
    care: AcceptedWorkProduct | None,
    safety: AcceptedWorkProduct | None,
    doctor_projection_allowed: bool,
    doctor_failure_codes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION,
        "source": source.model_dump(mode="json"),
        "runner_version": runner_version,
        "registry_hash": registry_hash,
        "runtime_request_sha256": runtime_request_sha256,
        "fact_snapshot_hash": fact_snapshot_hash,
        "summary_lines": list(summary_lines),
        "evidence_target_hash": evidence.target_hash,
        "care_target_hash": None if care is None else care.target_hash,
        "safety_target_hash": None if safety is None else safety.target_hash,
        "doctor_projection_allowed": doctor_projection_allowed,
        "doctor_failure_codes": list(doctor_failure_codes),
    }


class SharedNightAnalysis(StrictContract):
    """One accepted, role-neutral Product result before any communication pass."""

    schema_version: Literal["shared_night_analysis.v1"] = (
        SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION
    )
    source: SharedAnalysisSourceV1
    runner_version: str = PRODUCT_EPISODE_RUNNER_VERSION
    registry_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    runtime_request_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    shared_analysis_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    summary_lines: tuple[str, ...] = Field(min_length=1)
    evidence: AcceptedWorkProduct
    care: AcceptedWorkProduct | None = None
    safety: AcceptedWorkProduct | None = None
    doctor_projection_allowed: bool
    doctor_failure_codes: tuple[str, ...] = ()
    envelopes: tuple[AgentEnvelope, ...] = ()
    agent_invocations: tuple[AgentInvocationRecord, ...] = ()
    tool_receipts: tuple[ToolReceipt, ...] = ()

    @field_serializer("agent_invocations")
    def serialize_safe_agent_invocations(
        self,
        records: tuple[AgentInvocationRecord, ...],
    ) -> list[dict[str, Any]]:
        """Keep full request IDs out of analysis/attempt JSON.

        The worker copies them into the governed durable invocation-journal
        response before serializing this shared artifact.
        """

        return [
            record.model_dump(mode="json", exclude={"provider_request_id"})
            for record in records
        ]

    @classmethod
    def create(
        cls,
        *,
        source: SharedAnalysisSourceV1,
        registry_hash: str,
        runtime_request: ProductEpisodeRunRequest,
        summary_lines: tuple[str, ...],
        evidence: AcceptedWorkProduct,
        care: AcceptedWorkProduct | None,
        safety: AcceptedWorkProduct | None,
        doctor_projection_allowed: bool,
        doctor_failure_codes: tuple[str, ...] = (),
        envelopes: tuple[AgentEnvelope, ...] = (),
        agent_invocations: tuple[AgentInvocationRecord, ...] = (),
        tool_receipts: tuple[ToolReceipt, ...] = (),
    ) -> "SharedNightAnalysis":
        request_sha256 = product_episode_request_hash(runtime_request)
        material = _shared_analysis_hash_material(
            source=source,
            runner_version=PRODUCT_EPISODE_RUNNER_VERSION,
            registry_hash=registry_hash,
            runtime_request_sha256=request_sha256,
            fact_snapshot_hash=runtime_request.fact_snapshot.fact_snapshot_hash,
            summary_lines=summary_lines,
            evidence=evidence,
            care=care,
            safety=safety,
            doctor_projection_allowed=doctor_projection_allowed,
            doctor_failure_codes=doctor_failure_codes,
        )
        return cls(
            source=source,
            registry_hash=registry_hash,
            runtime_request_sha256=request_sha256,
            fact_snapshot_id=runtime_request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=runtime_request.fact_snapshot.fact_snapshot_hash,
            shared_analysis_sha256=stable_hash(material),
            summary_lines=summary_lines,
            evidence=evidence,
            care=care,
            safety=safety,
            doctor_projection_allowed=doctor_projection_allowed,
            doctor_failure_codes=doctor_failure_codes,
            envelopes=envelopes,
            agent_invocations=agent_invocations,
            tool_receipts=tool_receipts,
        )

    @model_validator(mode="after")
    def validate_shared_result(self) -> "SharedNightAnalysis":
        products = tuple(
            item for item in (self.evidence, self.care, self.safety) if item is not None
        )
        if any(item.fact_snapshot_hash != self.fact_snapshot_hash for item in products):
            raise ValueError("shared work products bind different FactSnapshots")
        if self.evidence.agent_id is not AgentId.EVIDENCE_REASONING:
            raise ValueError("shared analysis requires accepted EvidenceReasoning output")
        evidence = ProductEvidencePacket.model_validate(self.evidence.payload)
        care = None
        if self.care is not None:
            if self.care.agent_id is not AgentId.CARE_STRATEGY:
                raise ValueError("shared Care output has the wrong owner")
            care = ProductCareStrategy.model_validate(self.care.payload)
        decision = None
        if self.safety is not None:
            if self.safety.agent_id is not AgentId.SAFETY_REVIEW:
                raise ValueError("shared Safety output has the wrong owner")
            decision = ProductSafetyDecision.model_validate(self.safety.payload)
            allowed_targets = {
                self.evidence.target_hash,
                *(() if self.care is None else (self.care.target_hash,)),
            }
            if decision.review_target_hash not in allowed_targets:
                raise ValueError("shared Safety does not review an accepted target")
        if self.doctor_projection_allowed and (
            decision is None or decision.verdict is not SafetyVerdict.APPROVE
        ):
            raise ValueError("doctor projection requires approved Safety")
        if self.doctor_projection_allowed and self.doctor_failure_codes:
            raise ValueError("publishable doctor projection cannot carry failures")
        if not self.doctor_projection_allowed and not self.doctor_failure_codes:
            raise ValueError("blocked doctor projection requires a failure code")
        expected_lines = tuple(claim.statement for claim in evidence.claims)
        if care is not None and care.primary_action is not None:
            expected_lines = (*expected_lines, care.primary_action.title)
        if not expected_lines:
            expected_lines = ("当前没有足够证据形成健康趋势结论。",)
        if self.summary_lines != expected_lines:
            raise ValueError("shared summary must be derived from accepted facts")
        expected_hash = stable_hash(
            _shared_analysis_hash_material(
                source=self.source,
                runner_version=self.runner_version,
                registry_hash=self.registry_hash,
                runtime_request_sha256=self.runtime_request_sha256,
                fact_snapshot_hash=self.fact_snapshot_hash,
                summary_lines=self.summary_lines,
                evidence=self.evidence,
                care=self.care,
                safety=self.safety,
                doctor_projection_allowed=self.doctor_projection_allowed,
                doctor_failure_codes=self.doctor_failure_codes,
            )
        )
        if self.shared_analysis_sha256 != expected_hash:
            raise ValueError("shared analysis hash is inconsistent")
        return self

    def accepted_products(self) -> dict[WorkProductKind, AcceptedWorkProduct]:
        products = {WorkProductKind.EVIDENCE_PACKET: self.evidence}
        if self.care is not None:
            products[WorkProductKind.CARE_STRATEGY] = self.care
        if self.safety is not None:
            products[WorkProductKind.SAFETY_DECISION] = self.safety
        return products


def _projection_hash_material(
    *,
    role: ReportRole,
    state: RoleProjectionState,
    source_shared_analysis_sha256: str,
    data_sufficiency: Literal["sufficient", "partial"],
    quality_state: Literal["good", "partial"],
    risk_state: str,
    title: str,
    text: str | None,
    context_notice: str,
    claim_refs: tuple[str, ...],
    care_candidate_refs: tuple[str, ...],
    caveats: tuple[str, ...],
    safety_notices: tuple[str, ...],
    failure_codes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": ROLE_PROJECTION_SCHEMA_VERSION,
        "role": role.value,
        "state": state.value,
        "source_shared_analysis_sha256": source_shared_analysis_sha256,
        "data_sufficiency": data_sufficiency,
        "quality_state": quality_state,
        "risk_state": risk_state,
        "title": title,
        "text": text,
        "context_notice": context_notice,
        "claim_refs": list(claim_refs),
        "care_candidate_refs": list(care_candidate_refs),
        "caveats": list(caveats),
        "safety_notices": list(safety_notices),
        "failure_codes": list(failure_codes),
    }


class RoleProjection(StrictContract):
    """Deterministic role view derived from exactly one shared analysis."""

    schema_version: Literal["role_projection.v1"] = ROLE_PROJECTION_SCHEMA_VERSION
    role: ReportRole
    state: RoleProjectionState
    source_shared_analysis_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    projection_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    data_sufficiency: Literal["sufficient", "partial"]
    quality_state: Literal["good", "partial"]
    risk_state: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=200)
    text: str | None = Field(default=None, min_length=1, max_length=6000)
    context_notice: str = Field(..., min_length=1, max_length=500)
    claim_refs: tuple[str, ...] = ()
    care_candidate_refs: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    safety_notices: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_projection(self) -> "RoleProjection":
        if self.state is RoleProjectionState.READY:
            if self.text is None or self.failure_codes:
                raise ValueError("ready projection requires text without failures")
        elif self.role is not ReportRole.DOCTOR:
            raise ValueError("only the doctor projection can be policy blocked")
        elif self.text is not None or not self.failure_codes:
            raise ValueError("blocked doctor projection requires only failure codes")
        expected = stable_hash(
            _projection_hash_material(
                role=self.role,
                state=self.state,
                source_shared_analysis_sha256=self.source_shared_analysis_sha256,
                data_sufficiency=self.data_sufficiency,
                quality_state=self.quality_state,
                risk_state=self.risk_state,
                title=self.title,
                text=self.text,
                context_notice=self.context_notice,
                claim_refs=self.claim_refs,
                care_candidate_refs=self.care_candidate_refs,
                caveats=self.caveats,
                safety_notices=self.safety_notices,
                failure_codes=self.failure_codes,
            )
        )
        if self.projection_sha256 != expected:
            raise ValueError("role projection hash is inconsistent")
        return self


_SHARED_REPORT_SAFETY_NOTICES = (
    "本报告由 AI 辅助整理，内容来自结构化证据。",
    "本报告仅用于睡眠健康观察。",
    "本报告不构成临床诊断或医疗建议。",
    "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
)


def build_role_projection_runtime_manifest(
    *,
    projection_policy_version: str = "shared_role_projection.v1",
) -> dict[str, Any]:
    """Canonical deterministic projection pins, independent of analysis."""

    return {
        "schema_version": "role_projection_runtime_manifest.v1",
        "projection_schema": ROLE_PROJECTION_SCHEMA_VERSION,
        "projection_policy_version": projection_policy_version,
        "projector_version": "build_shared_role_projections.v1",
        "safety_notices_sha256": stable_hash(_SHARED_REPORT_SAFETY_NOTICES),
    }


def role_projection_identity_sha256(
    *,
    desired_analysis_sha256: str,
    role: ReportRole,
    projection_sha256: str,
    projection_manifest_sha256: str,
) -> str:
    """Bind one deterministic role artifact to analysis and projection pins."""

    return stable_hash(
        {
            "schema_version": "role_projection_identity.v1",
            "desired_analysis_sha256": desired_analysis_sha256,
            "role": role.value,
            "projection_sha256": projection_sha256,
            "projection_manifest_sha256": projection_manifest_sha256,
        }
    )


def build_shared_role_projections(
    shared: SharedNightAnalysis,
) -> tuple[RoleProjection, RoleProjection, RoleProjection]:
    """Build all three deterministic views without invoking a model."""

    evidence = ProductEvidencePacket.model_validate(shared.evidence.payload)
    care = (
        None
        if shared.care is None
        else ProductCareStrategy.model_validate(shared.care.payload)
    )
    claim_refs = tuple(claim.claim_id for claim in evidence.claims)
    care_refs = (
        ()
        if care is None or care.primary_action is None
        else (care.primary_action.candidate_id,)
    )
    caveats = tuple(
        dict.fromkeys(
            (
                *shared.source.limitations,
                *((shared.source.partial_caveat,) if shared.source.partial_caveat else ()),
                "仅作睡眠健康观察参考，不构成诊断。",
            )
        )
    )
    projections: list[RoleProjection] = []
    for role in REPORT_ROLE_ORDER:
        title = {
            ReportRole.ELDER: "昨夜睡眠观察",
            ReportRole.FAMILY: "家属照护摘要",
            ReportRole.DOCTOR: "睡眠观察证据摘要",
        }[role]
        context_notice = {
            ReportRole.ELDER: "内容仅覆盖当前授权范围内的已验收信息。",
            ReportRole.FAMILY: "内容仅供授权家属在当前范围内了解。",
            ReportRole.DOCTOR: "材料仅包含当前授权范围内的已验收来源。",
        }[role]
        role_caveats = caveats
        if role is ReportRole.DOCTOR:
            role_caveats = tuple(
                dict.fromkeys(
                    (
                        *caveats,
                        "毫米波雷达不能替代 PSG、病史、查体或临床判断。",
                    )
                )
            )
        if role is ReportRole.DOCTOR and not shared.doctor_projection_allowed:
            state = RoleProjectionState.POLICY_BLOCKED
            text = None
            failure_codes = shared.doctor_failure_codes
        else:
            state = RoleProjectionState.READY
            failure_codes = ()
            heading = {
                ReportRole.ELDER: (
                    "我们根据昨夜设备记录，为您整理了这些观察："
                ),
                ReportRole.FAMILY: "以下内容用于家属连续观察与照护协同：",
                ReportRole.DOCTOR: "以下为已验收睡眠观察摘要：",
            }[role]
            lines = [heading, *[f"- {line}" for line in shared.summary_lines]]
            if shared.source.partial_caveat:
                lines.extend(("数据说明：", f"- {shared.source.partial_caveat}"))
            lines.extend(
                (
                    "观察边界：",
                    *[f"- {item}" for item in role_caveats],
                    "安全说明：",
                    *[f"- {item}" for item in _SHARED_REPORT_SAFETY_NOTICES],
                )
            )
            text = "\n".join(lines)
        material = _projection_hash_material(
            role=role,
            state=state,
            source_shared_analysis_sha256=shared.shared_analysis_sha256,
            data_sufficiency=shared.source.data_sufficiency,
            quality_state=shared.source.quality_state,
            risk_state=shared.source.risk_state,
            title=title,
            text=text,
            context_notice=context_notice,
            claim_refs=claim_refs,
            care_candidate_refs=care_refs,
            caveats=role_caveats,
            safety_notices=_SHARED_REPORT_SAFETY_NOTICES,
            failure_codes=failure_codes,
        )
        projections.append(
            RoleProjection(
                role=role,
                state=state,
                source_shared_analysis_sha256=shared.shared_analysis_sha256,
                projection_sha256=stable_hash(material),
                data_sufficiency=shared.source.data_sufficiency,
                quality_state=shared.source.quality_state,
                risk_state=shared.source.risk_state,
                title=title,
                text=text,
                context_notice=context_notice,
                claim_refs=claim_refs,
                care_candidate_refs=care_refs,
                caveats=role_caveats,
                safety_notices=_SHARED_REPORT_SAFETY_NOTICES,
                failure_codes=failure_codes,
            )
        )
    return tuple(projections)  # type: ignore[return-value]


class ElderNarrativeRequest(StrictContract):
    schema_version: Literal["elder_narrative_request.v1"] = (
        ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION
    )
    runtime_request: ProductEpisodeRunRequest
    shared_analysis: SharedNightAnalysis
    elder_projection: RoleProjection
    source_projection_identity_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
    )
    render_manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    render_identity_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @property
    def episode_id(self) -> str:
        return self.runtime_request.episode_id

    @classmethod
    def create(
        cls,
        *,
        runtime_request: ProductEpisodeRunRequest,
        shared_analysis: SharedNightAnalysis,
        elder_projection: RoleProjection,
        render_manifest_sha256: str,
        source_projection_identity_sha256: str | None = None,
    ) -> "ElderNarrativeRequest":
        projection_identity = (
            elder_projection.projection_sha256
            if source_projection_identity_sha256 is None
            else source_projection_identity_sha256
        )
        identity = stable_hash(
            {
                "schema_version": ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION,
                "shared_analysis_sha256": shared_analysis.shared_analysis_sha256,
                "elder_projection_sha256": projection_identity,
                "render_manifest_sha256": render_manifest_sha256,
            }
        )
        return cls(
            runtime_request=runtime_request,
            shared_analysis=shared_analysis,
            elder_projection=elder_projection,
            source_projection_identity_sha256=projection_identity,
            render_manifest_sha256=render_manifest_sha256,
            render_identity_sha256=identity,
        )

    @model_validator(mode="after")
    def validate_narrative_request(self) -> "ElderNarrativeRequest":
        request = self.runtime_request
        projection = self.elder_projection
        if request.episode_type.value != "morning_review":
            raise ValueError("elder narrative requires MORNING_REVIEW")
        if request.audience_role != "elder" or request.doctor_material:
            raise ValueError("elder narrative requires exactly the elder audience")
        if request.user_text or request.user_fact_responses:
            raise ValueError("elder narrative cannot consume conversational input")
        if request.fact_snapshot.fact_snapshot_hash != self.shared_analysis.fact_snapshot_hash:
            raise ValueError("elder narrative FactSnapshot differs from shared analysis")
        if (
            projection.role is not ReportRole.ELDER
            or projection.state is not RoleProjectionState.READY
            or projection.source_shared_analysis_sha256
            != self.shared_analysis.shared_analysis_sha256
        ):
            raise ValueError("elder narrative requires the ready shared elder projection")
        expected = stable_hash(
            {
                "schema_version": ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION,
                "shared_analysis_sha256": self.shared_analysis.shared_analysis_sha256,
                "elder_projection_sha256": (
                    self.source_projection_identity_sha256
                ),
                "render_manifest_sha256": self.render_manifest_sha256,
            }
        )
        if self.render_identity_sha256 != expected:
            raise ValueError("elder narrative render identity is inconsistent")
        return self


class ElderNarrative(StrictContract):
    schema_version: Literal["elder_narrative.v1"] = ELDER_NARRATIVE_SCHEMA_VERSION
    state: ElderNarrativeState
    source_shared_analysis_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_projection_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    render_identity_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    text: str | None = Field(default=None, min_length=1, max_length=6000)
    communication: CommunicationDraft | None = None
    invocation: AgentInvocationRecord | None = None
    failure_codes: tuple[str, ...] = ()

    @field_serializer("invocation")
    def serialize_safe_invocation(
        self,
        record: AgentInvocationRecord | None,
    ) -> dict[str, Any] | None:
        """Keep full request IDs only in the governed invocation journal."""

        if record is None:
            return None
        return record.model_dump(mode="json", exclude={"provider_request_id"})

    @model_validator(mode="after")
    def validate_narrative(self) -> "ElderNarrative":
        if self.invocation is not None and self.invocation.agent_id is not AgentId.SLEEP_CARE:
            raise ValueError("elder narrative invocation must belong to SleepCare")
        if self.state is ElderNarrativeState.READY:
            if (
                self.communication is None
                or self.invocation is None
                or self.text != self.communication.text
                or self.communication.audience_role != "elder"
                or self.failure_codes
            ):
                raise ValueError("ready elder narrative is incomplete")
        elif self.state is ElderNarrativeState.FALLBACK:
            if self.text is None or self.communication is not None or not self.failure_codes:
                raise ValueError("fallback elder narrative requires safe fallback text")
        elif self.text is not None or self.communication is not None:
            raise ValueError("failed/stale elder narrative cannot carry content")
        return self


__all__ = [
    "ELDER_NARRATIVE_REQUEST_SCHEMA_VERSION",
    "ELDER_NARRATIVE_SCHEMA_VERSION",
    "REPORT_ROLE_ORDER",
    "ROLE_PROJECTION_SCHEMA_VERSION",
    "SHARED_ANALYSIS_SOURCE_SCHEMA_VERSION",
    "SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION",
    "ElderNarrative",
    "ElderNarrativeRequest",
    "ElderNarrativeState",
    "ReportRole",
    "RoleProjection",
    "RoleProjectionState",
    "RoleReportArtifact",
    "RoleReportBundle",
    "SharedAnalysisRunRequest",
    "SharedAnalysisSourceV1",
    "SharedNightAnalysis",
    "build_elder_narrative_runtime_manifest",
    "build_role_projection_runtime_manifest",
    "build_role_report_templates",
    "build_safe_model_pin",
    "build_shared_analysis_runtime_manifest",
    "build_shared_role_projections",
    "role_projection_identity_sha256",
]
