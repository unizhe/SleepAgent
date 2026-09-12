"""角色材料渲染与报告合同。"""

from __future__ import annotations


# 合并自 reports/roles.py。
"""定义角色报告的稳定角色合同，不负责报告渲染。"""

from enum import Enum
import re

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
    model_validator,
)

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
from datetime import date, datetime, timezone
from typing import Any, Final, Literal, Protocol, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        "render_policy_version": "elder_narrative_render.v2",
        "rewrite_policy_version": "bounded_atom_selection.v1",
        "numeric_rendering_policy": "runtime_defined_display_only.v1",
        "profile": dict(profile),
        "skill": dict(skill),
        "model": dict(model),
        "content_plan_assembly": content_plan_assembly,
    }


SHARED_ANALYSIS_SOURCE_SCHEMA_VERSION = "shared_analysis_source.v1"
SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION = "shared_night_analysis.v1"
ROLE_PROJECTION_SCHEMA_VERSION = "role_projection.v1"
REPORTING_CONTEXT_SCHEMA_VERSION: Final[Literal["reporting_context.v1"]] = (
    "reporting_context.v1"
)
REPORT_SEMANTIC_FACT_SCHEMA_VERSION: Final[Literal["report_semantic_fact.v1"]] = (
    "report_semantic_fact.v1"
)
ZH_CN_ROLE_RENDERER_VERSION = "zh_cn_role_renderer.v2"
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


class ReportingContextV1(StrictContract):
    """Pinned time, locale, audience, and renderer authority for one report."""

    schema_version: Literal["reporting_context.v1"] = (
        REPORTING_CONTEXT_SCHEMA_VERSION
    )
    timezone_name: str = Field(..., min_length=1)
    locale: Literal["zh-CN"] = "zh-CN"
    audience: Literal["shared", "elder", "family", "doctor"] = "shared"
    authoritative_start_at_utc: datetime
    authoritative_end_at_utc: datetime
    local_sleep_date: date
    renderer_version: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_reporting_authority(self) -> "ReportingContextV1":
        try:
            zone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("reporting timezone must be a valid IANA timezone") from exc
        for value, label in (
            (self.authoritative_start_at_utc, "authoritative_start_at_utc"),
            (self.authoritative_end_at_utc, "authoritative_end_at_utc"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware")
            if value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError(f"{label} must be normalized to UTC")
        if self.authoritative_end_at_utc <= self.authoritative_start_at_utc:
            raise ValueError("reporting end boundary must follow start boundary")
        if self.authoritative_end_at_utc.astimezone(zone).date() != self.local_sleep_date:
            raise ValueError("local sleep date must be owned by the local end boundary")
        return self

    def for_audience(
        self,
        audience: Literal["elder", "family", "doctor"],
        *,
        renderer_version: str = ZH_CN_ROLE_RENDERER_VERSION,
    ) -> "ReportingContextV1":
        return cast(
            ReportingContextV1,
            self.model_copy(
                update={
                    "audience": audience,
                    "renderer_version": renderer_version,
                }
            ),
        )

    def semantic_time_material(self) -> dict[str, Any]:
        """Return meaning that changes facts, excluding wording/projection pins."""

        return {
            "timezone_name": self.timezone_name,
            "authoritative_start_at_utc": self.authoritative_start_at_utc,
            "authoritative_end_at_utc": self.authoritative_end_at_utc,
            "local_sleep_date": self.local_sleep_date,
        }


class ReportSemanticFact(StrictContract):
    """Structured reporting fact; prose is never its authority."""

    schema_version: Literal["report_semantic_fact.v1"] = (
        REPORT_SEMANTIC_FACT_SCHEMA_VERSION
    )
    fact_id: str = Field(..., pattern=r"^report-fact:[0-9a-f]{32}$")
    fact_kind: Literal[
        "direct_metric",
        "accepted_claim",
        "quality",
        "care_candidate",
    ]
    metric_id: str = Field(..., min_length=1)
    value: str | int | float | bool | None = None
    unit: str | None = Field(default=None, min_length=1)
    window: str | None = Field(default=None, min_length=1)
    comparison: str | None = Field(default=None, min_length=1)
    quality_qualifier: str | None = Field(default=None, min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    authority: str = Field(..., min_length=1)
    caveat: str | None = Field(default=None, min_length=1)

    @classmethod
    def create(cls, **values: Any) -> "ReportSemanticFact":
        material = dict(values)
        material.pop("fact_id", None)
        candidate = cls.model_construct(
            fact_id="report-fact:" + "0" * 32,
            **material,
        )
        normalized = candidate.model_dump(mode="json", exclude={"fact_id"})
        return cls(
            fact_id="report-fact:" + stable_hash(normalized)[:32],
            **material,
        )

    @model_validator(mode="after")
    def validate_fact_identity(self) -> "ReportSemanticFact":
        expected = "report-fact:" + stable_hash(
            self.model_dump(mode="json", exclude={"fact_id"})
        )[:32]
        if self.fact_id != expected:
            raise ValueError("report semantic fact ID is inconsistent")
        if self.fact_kind == "direct_metric" and self.value is None:
            raise ValueError("direct metric fact requires a value")
        return self


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
    reporting_context: ReportingContextV1 | None = None

    @model_validator(mode="after")
    def require_partial_caveat(self) -> "SharedAnalysisSourceV1":
        partial = self.data_sufficiency == "partial" or self.quality_state == "partial"
        if partial != (self.partial_caveat is not None):
            raise ValueError("PARTIAL shared analysis requires exactly one caveat")
        if self.reporting_context is not None:
            if self.reporting_context.audience != "shared":
                raise ValueError("shared source requires a shared reporting audience")
            if self.reporting_context.local_sleep_date != self.wake_date:
                raise ValueError("reporting context local date must match wake date")
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
        if self.source.reporting_context is None:
            raise ValueError("new shared analysis requires a reporting context")
        if (
            request.fact_snapshot.canonical_data_version
            != self.source.canonical_data_version
        ):
            raise ValueError("shared analysis canonical source mismatch")
        if request.fact_snapshot.source_scope.date_end != self.source.wake_date:
            raise ValueError("shared analysis wake-date scope mismatch")
        return self


def build_report_semantic_facts(
    *,
    runtime_request: ProductEpisodeRunRequest,
    evidence: AcceptedWorkProduct,
    care: AcceptedWorkProduct | None,
    source: SharedAnalysisSourceV1,
) -> tuple[ReportSemanticFact, ...]:
    """Build deterministic facts without treating Agent prose as data."""

    packet = ProductEvidencePacket.model_validate(evidence.payload)
    default_refs = tuple(runtime_request.fact_snapshot.source_refs) or (
        evidence.work_product_ref,
    )
    quality = source.quality_state
    facts: list[ReportSemanticFact] = []

    night_input = runtime_request.tool_inputs.get("radar.get_night_evidence", {})
    night_data = night_input.get("data")
    if isinstance(night_data, Mapping):
        summary = night_data.get("deterministic_night_summary")
        if isinstance(summary, Mapping):
            stage_minutes = summary.get("stage_minutes")
            has_stage_metrics = isinstance(stage_minutes, Mapping) and bool(
                stage_minutes
            )
            if (
                has_stage_metrics
                or summary.get("sleep_window_minutes") is not None
            ) and summary.get("schema_version") != (
                "product_deterministic_night_summary.v2"
            ):
                raise ValueError(
                    "stage facts require product deterministic summary v2"
                )
            sleep_window = _validated_sleep_stage_window(
                summary,
                required=has_stage_metrics,
            )
            if sleep_window is not None:
                stage_start, stage_end, duration = sleep_window
                for metric_id, value in (
                    ("sleep_stage_coverage_start_at", stage_start.isoformat()),
                    ("sleep_stage_coverage_end_at", stage_end.isoformat()),
                ):
                    facts.append(
                        ReportSemanticFact.create(
                            fact_kind="direct_metric",
                            metric_id=metric_id,
                            value=value,
                            window="effective_sleep_stage_coverage",
                            quality_qualifier=quality,
                            source_refs=default_refs,
                            authority="canonical_sleep_stage_intervals",
                        )
                    )
                facts.append(
                    ReportSemanticFact.create(
                        fact_kind="direct_metric",
                        metric_id="sleep_window_minutes",
                        value=duration,
                        unit="minutes",
                        window="effective_sleep_stage_coverage",
                        quality_qualifier=quality,
                        source_refs=default_refs,
                        authority="canonical_sleep_stage_intervals",
                    )
                )
            scalar_metrics = (
                ("bed_exit_count", summary.get("bed_exit_count"), "count"),
            )
            for metric_id, value, unit in scalar_metrics:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    facts.append(
                        ReportSemanticFact.create(
                            fact_kind="direct_metric",
                            metric_id=metric_id,
                            value=value,
                            unit=unit,
                            window="authoritative_observation_window",
                            quality_qualifier=quality,
                            source_refs=default_refs,
                            authority="deterministic_product_summary",
                        )
                    )
            if isinstance(stage_minutes, Mapping):
                for stage, value in sorted(stage_minutes.items()):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        facts.append(
                            ReportSemanticFact.create(
                                fact_kind="direct_metric",
                                metric_id=f"sleep_stage.{stage}_minutes",
                                value=value,
                                unit="minutes",
                                window="effective_sleep_stage_coverage",
                                quality_qualifier=quality,
                                source_refs=default_refs,
                                authority="canonical_sleep_stage_intervals",
                            )
                        )
            vital_centers = summary.get("vital_centers")
            if isinstance(vital_centers, Mapping):
                vital_units = {
                    "heart_rate": "bpm",
                    "respiratory_rate": "breaths_per_minute",
                }
                for metric_id, unit in vital_units.items():
                    value = vital_centers.get(metric_id)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        facts.append(
                            ReportSemanticFact.create(
                                fact_kind="direct_metric",
                                metric_id=f"{metric_id}_mean",
                                value=value,
                                unit=unit,
                                window="authoritative_observation_window",
                                quality_qualifier=quality,
                                source_refs=default_refs,
                                authority="canonical_device_observations",
                            )
                        )

    for claim in packet.claims:
        claim_refs = tuple(claim.evidence_refs) or default_refs
        facts.append(
            ReportSemanticFact.create(
                fact_kind="accepted_claim",
                metric_id=claim.metric_id or f"claim.{claim.semantic.value}",
                comparison=claim.semantic.value,
                quality_qualifier=quality,
                source_refs=claim_refs,
                authority=claim.source_kind.value,
                caveat=(
                    "inference_has_alternatives"
                    if claim.alternative_explanations
                    else None
                ),
            )
        )

    coverage = packet.coverage_ratio
    facts.append(
        ReportSemanticFact.create(
            fact_kind="quality",
            metric_id="data_coverage_ratio",
            value=coverage,
            unit="ratio" if coverage is not None else None,
            quality_qualifier=quality,
            source_refs=default_refs,
            authority="accepted_evidence_packet",
            caveat=source.partial_caveat,
        )
    )
    if care is not None:
        strategy = ProductCareStrategy.model_validate(care.payload)
        if strategy.primary_action is not None:
            facts.append(
                ReportSemanticFact.create(
                    fact_kind="care_candidate",
                    metric_id="care_candidate.pending_confirmation",
                    value=True,
                    quality_qualifier=quality,
                    source_refs=tuple(
                        strategy.primary_action.rationale_evidence_refs
                    )
                    or default_refs,
                    authority="accepted_care_strategy",
                    caveat="candidate_not_executed",
                )
            )
    return tuple(facts)


def _validated_sleep_stage_window(
    summary: Mapping[str, Any],
    *,
    required: bool,
) -> tuple[datetime, datetime, float] | None:
    """Read one internally consistent UTC stage-coverage window."""

    raw_start = summary.get("sleep_window_start")
    raw_end = summary.get("sleep_window_end")
    raw_minutes = summary.get("sleep_window_minutes")
    if raw_start is None and raw_end is None and raw_minutes is None:
        if required:
            raise ValueError("stage metrics require a sleep-stage coverage window")
        return None
    if (
        not isinstance(raw_start, str)
        or not isinstance(raw_end, str)
        or not isinstance(raw_minutes, (int, float))
        or isinstance(raw_minutes, bool)
    ):
        raise ValueError("sleep-stage coverage requires typed start, end, and minutes")
    start = _report_datetime(raw_start)
    end = _report_datetime(raw_end)
    if start is None or end is None or end <= start:
        raise ValueError("sleep-stage coverage requires valid aware boundaries")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    expected = round((end - start).total_seconds() / 60, 1)
    if float(raw_minutes) != expected:
        raise ValueError("sleep-stage coverage duration is inconsistent")
    return start, end, float(raw_minutes)


def _report_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


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
    semantic_facts: tuple[ReportSemanticFact, ...] = (),
    semantic_hash_version: Literal[
        "legacy_summary.v1", "structured_facts.v1"
    ] = "legacy_summary.v1",
) -> dict[str, Any]:
    if semantic_hash_version == "structured_facts.v1":
        if source.reporting_context is None:
            raise ValueError("structured shared hash requires reporting context")
        source_material = source.model_dump(
            mode="json",
            exclude={"reporting_context"},
        )
        return {
            "schema_version": SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION,
            "semantic_hash_version": semantic_hash_version,
            "source": source_material,
            "reporting_time_authority": (
                source.reporting_context.semantic_time_material()
            ),
            "runner_version": runner_version,
            "registry_hash": registry_hash,
            "runtime_request_sha256": runtime_request_sha256,
            "fact_snapshot_hash": fact_snapshot_hash,
            "semantic_facts": [
                item.model_dump(mode="json") for item in semantic_facts
            ],
            "evidence_target_hash": evidence.target_hash,
            "care_target_hash": None if care is None else care.target_hash,
            "safety_target_hash": None if safety is None else safety.target_hash,
            "doctor_projection_allowed": doctor_projection_allowed,
            "doctor_failure_codes": list(doctor_failure_codes),
        }
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
    semantic_hash_version: Literal[
        "legacy_summary.v1", "structured_facts.v1"
    ] = "legacy_summary.v1"
    semantic_facts: tuple[ReportSemanticFact, ...] = ()
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
        semantic_facts: tuple[ReportSemanticFact, ...] | None = None,
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
        resolved_facts = (
            build_report_semantic_facts(
                runtime_request=runtime_request,
                evidence=evidence,
                care=care,
                source=source,
            )
            if semantic_facts is None
            else semantic_facts
        )
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
            semantic_facts=resolved_facts,
            semantic_hash_version="structured_facts.v1",
        )
        return cls(
            source=source,
            registry_hash=registry_hash,
            runtime_request_sha256=request_sha256,
            fact_snapshot_id=runtime_request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=runtime_request.fact_snapshot.fact_snapshot_hash,
            shared_analysis_sha256=stable_hash(material),
            summary_lines=summary_lines,
            semantic_hash_version="structured_facts.v1",
            semantic_facts=resolved_facts,
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
        if self.semantic_hash_version == "structured_facts.v1":
            if self.source.reporting_context is None or not self.semantic_facts:
                raise ValueError(
                    "structured shared analysis requires context and semantic facts"
                )
            _validate_sleep_stage_semantic_facts(
                self.semantic_facts,
                self.source.reporting_context,
            )
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
                semantic_facts=self.semantic_facts,
                semantic_hash_version=self.semantic_hash_version,
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


def _validate_sleep_stage_semantic_facts(
    facts: tuple[ReportSemanticFact, ...],
    context: ReportingContextV1,
) -> None:
    """Reject stage facts that are detached from their authoritative scope."""

    direct = {
        item.metric_id: item
        for item in facts
        if item.fact_kind == "direct_metric"
    }
    stage_facts = tuple(
        item for key, item in direct.items()
        if key.startswith("sleep_stage.")
    )
    duration = direct.get("sleep_window_minutes")
    start_fact = direct.get("sleep_stage_coverage_start_at")
    end_fact = direct.get("sleep_stage_coverage_end_at")
    if not stage_facts and duration is None and start_fact is None and end_fact is None:
        return
    if duration is None or start_fact is None or end_fact is None:
        raise ValueError("sleep-stage facts require one complete coverage window")
    start = _report_datetime(start_fact.value)
    end = _report_datetime(end_fact.value)
    if (
        start is None
        or end is None
        or end <= start
        or not isinstance(duration.value, (int, float))
        or isinstance(duration.value, bool)
    ):
        raise ValueError("sleep-stage semantic coverage is malformed")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    expected = round((end - start).total_seconds() / 60, 1)
    if float(duration.value) != expected:
        raise ValueError("sleep-stage semantic duration is inconsistent")
    if (
        start < context.authoritative_start_at_utc
        or end > context.authoritative_end_at_utc
    ):
        raise ValueError("sleep-stage coverage exceeds authoritative scope")
    window_facts = (duration, start_fact, end_fact, *stage_facts)
    if any(
        item.window != "effective_sleep_stage_coverage"
        for item in window_facts
    ):
        raise ValueError("sleep-stage facts silently conflate reporting windows")


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
    presentation_authority_sha256: str | None = None,
    reporting_context: ReportingContextV1 | None = None,
) -> dict[str, Any]:
    material = {
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
    if presentation_authority_sha256 is not None:
        material["presentation_authority_sha256"] = (
            presentation_authority_sha256
        )
    if reporting_context is not None:
        material["reporting_context"] = reporting_context.model_dump(mode="json")
    return material


_ELDER_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?"
)
_ELDER_FORBIDDEN_PUBLIC_TERMS = (
    "本次已验收信息：",
    "HIPAA",
    "FDA",
    "pull-backfilled",
    "reconstructed cadence",
    "context_notice",
)


class ElderNumericBinding(StrictContract):
    binding_key: str = Field(..., min_length=1, max_length=80)
    value: int | float
    unit: Literal["minutes", "count"]
    display_value: str = Field(..., min_length=1, max_length=40)
    approximation: Literal["exact", "about"] = "exact"

    @model_validator(mode="after")
    def validate_runtime_display(self) -> "ElderNumericBinding":
        if self.value < 0:
            raise ValueError("Elder numeric binding cannot be negative")
        if self.unit == "count" and (
            not isinstance(self.value, int)
            or self.display_value != str(self.value)
            or self.approximation != "exact"
        ):
            raise ValueError("Elder count display must preserve the exact integer")
        if self.unit == "minutes" and self.display_value != _elder_display_number(
            float(self.value)
        ):
            raise ValueError("Elder minute display must use Runtime rounding")
        return self


class ElderAtomRendering(StrictContract):
    rendering_id: str = Field(..., min_length=1, max_length=100)
    text: str = Field(..., min_length=1, max_length=1600)


class ElderMessageAtom(StrictContract):
    """Runtime-owned Elder meaning with a finite set of safe zh-CN renderings."""

    atom_id: str = Field(..., pattern=r"^elder-atom:[0-9a-f]{32}$")
    semantic_type: Literal[
        "sleep_stage_summary",
        "bed_exit_observation",
        "quality_caveat",
        "observation_boundary",
    ]
    priority: int = Field(..., ge=1, le=100)
    mandatory: bool
    numeric_bindings: tuple[ElderNumericBinding, ...] = ()
    localized_display_values: dict[str, str] = Field(default_factory=dict)
    source_refs: tuple[str, ...] = Field(min_length=1)
    quality_classification: Literal["good", "partial", "not_applicable"]
    risk_state: str = Field(..., min_length=1, max_length=100)
    care_state: Literal["not_presented"] = "not_presented"
    safety_classification: Literal[
        "observational",
        "non_diagnostic_boundary",
    ]
    allowed_elder_meaning: str = Field(..., min_length=1, max_length=800)
    renderings: tuple[ElderAtomRendering, ...] = Field(min_length=1, max_length=4)
    fallback_rendering_id: str = Field(..., min_length=1, max_length=100)

    @classmethod
    def create(
        cls,
        *,
        semantic_type: Literal[
            "sleep_stage_summary",
            "bed_exit_observation",
            "quality_caveat",
            "observation_boundary",
        ],
        priority: int,
        mandatory: bool,
        numeric_bindings: tuple[ElderNumericBinding, ...] = (),
        localized_display_values: dict[str, str] | None = None,
        source_refs: tuple[str, ...],
        quality_classification: Literal["good", "partial", "not_applicable"],
        risk_state: str,
        safety_classification: Literal[
            "observational",
            "non_diagnostic_boundary",
        ],
        renderings: tuple[ElderAtomRendering, ...],
        fallback_rendering_id: str,
        allowed_elder_meaning: str,
    ) -> "ElderMessageAtom":
        authority = {
            "semantic_type": semantic_type,
            "priority": priority,
            "mandatory": mandatory,
            "numeric_bindings": [
                item.model_dump(mode="json") for item in numeric_bindings
            ],
            "localized_display_values": localized_display_values or {},
            "source_refs": list(source_refs),
            "quality_classification": quality_classification,
            "risk_state": risk_state,
            "care_state": "not_presented",
            "safety_classification": safety_classification,
            "allowed_elder_meaning": allowed_elder_meaning,
            "renderings": [item.model_dump(mode="json") for item in renderings],
            "fallback_rendering_id": fallback_rendering_id,
        }
        return cls(
            atom_id="elder-atom:" + stable_hash(authority)[:32],
            **authority,
        )

    @model_validator(mode="after")
    def validate_bounded_renderings(self) -> "ElderMessageAtom":
        expected_atom_id = "elder-atom:" + stable_hash(
            self.model_dump(mode="json", exclude={"atom_id"})
        )[:32]
        if self.atom_id != expected_atom_id:
            raise ValueError("Elder atom ID is inconsistent with its authority")
        rendering_ids = tuple(item.rendering_id for item in self.renderings)
        if len(rendering_ids) != len(set(rendering_ids)):
            raise ValueError("Elder atom rendering IDs must be unique")
        if self.fallback_rendering_id not in rendering_ids:
            raise ValueError("Elder atom fallback rendering is unavailable")
        binding_keys = tuple(item.binding_key for item in self.numeric_bindings)
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("Elder numeric binding keys must be unique")
        allowed_numbers = {
            token
            for value in (
                *(
                    item.display_value
                    for item in self.numeric_bindings
                ),
                *self.localized_display_values.values(),
            )
            for token in _ELDER_NUMBER_PATTERN.findall(value)
        }
        for rendering in self.renderings:
            if not re.search(r"[\u3400-\u9fff]", rendering.text):
                raise ValueError("Elder atom rendering must be zh-CN-compatible")
            if any(
                forbidden.lower() in rendering.text.lower()
                for forbidden in _ELDER_FORBIDDEN_PUBLIC_TERMS
            ):
                raise ValueError("Elder atom rendering exposes internal prose")
            rendered_numbers = set(
                _ELDER_NUMBER_PATTERN.findall(rendering.text)
            )
            if not rendered_numbers.issubset(allowed_numbers):
                raise ValueError("Elder atom rendering has an unsupported number")
            if any(
                item.display_value not in rendering.text
                for item in self.numeric_bindings
            ):
                raise ValueError(
                    "Elder atom rendering omitted a Runtime numeric display"
                )
        if any(
            "utc" in value.lower()
            for value in self.localized_display_values.values()
        ):
            raise ValueError("Elder localized display cannot expose UTC")
        return self

    def rendering(self, rendering_id: str) -> ElderAtomRendering:
        try:
            return next(
                item for item in self.renderings
                if item.rendering_id == rendering_id
            )
        except StopIteration as exc:
            raise ValueError("Elder atom rendering is not authorized") from exc


def elder_presentation_authority_sha256(
    atoms: tuple[ElderMessageAtom, ...],
) -> str:
    if not atoms:
        raise ValueError("Elder presentation authority requires message atoms")
    return stable_hash([item.model_dump(mode="json") for item in atoms])


def deterministic_elder_fallback(
    atoms: tuple[ElderMessageAtom, ...],
) -> str:
    mandatory = tuple(item for item in atoms if item.mandatory)
    if not mandatory:
        raise ValueError("deterministic Elder fallback requires mandatory atoms")
    ordered = sorted(mandatory, key=lambda item: (item.priority, item.atom_id))
    return "\n\n".join(
        item.rendering(item.fallback_rendering_id).text
        for item in ordered
    )


def _elder_display_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


class _ElderPresentationFactsPort(Protocol):
    source_refs: tuple[str, ...]
    stage_observation_minutes: float
    presented_stage_local_display: str | None
    stage_boundary_state: str
    classified_stage_minutes: dict[str, float]
    classified_totals_state: str
    bed_exit_count: int
    bed_exit_local_times: tuple[str, ...]
    invalid_interval_count: int
    out_of_episode_interval_count: int


def build_elder_message_atoms(
    shared: SharedNightAnalysis,
    presentation: _ElderPresentationFactsPort,
) -> tuple[ElderMessageAtom, ...]:
    """Build the small, role-filtered Elder authority from accepted analysis."""

    authority_refs = tuple(
        dict.fromkeys(
            (
                f"shared_analysis:sha256:{shared.shared_analysis_sha256}",
                shared.evidence.work_product_ref,
                *presentation.source_refs,
            )
        )
    )
    risk_state = shared.source.risk_state
    atoms: list[ElderMessageAtom] = []

    summary_bindings: list[ElderNumericBinding] = []
    localized_values: dict[str, str] = {}
    if presentation.stage_observation_minutes > 0:
        observed = _elder_display_number(
            presentation.stage_observation_minutes
        )
        summary_bindings.append(
            ElderNumericBinding(
                binding_key="stage_observation_minutes",
                value=presentation.stage_observation_minutes,
                unit="minutes",
                display_value=observed,
                approximation="about",
            )
        )
        local_span = presentation.presented_stage_local_display
        if local_span is not None:
            localized_values["stage_observation_local_span"] = local_span
        constrained = (
            presentation.stage_boundary_state == "constrained_to_episode"
        )
        span_phrase = (
            ""
            if local_span is None
            else (
                f"按夜间时段范围，设备在{local_span}"
                if constrained
                else f"设备在{local_span}"
            )
        )
        if not span_phrase:
            span_phrase = "设备"
        stage_parts: list[str] = []
        stage_names = {
            "light": "浅睡",
            "deep": "深睡",
            "rem": "REM 睡眠",
        }
        if presentation.classified_totals_state == "reliable":
            for stage in ("light", "deep", "rem"):
                minutes = presentation.classified_stage_minutes.get(stage, 0.0)
                if minutes <= 0:
                    continue
                displayed = _elder_display_number(minutes)
                summary_bindings.append(
                    ElderNumericBinding(
                        binding_key=f"stage_minutes.{stage}",
                        value=minutes,
                        unit="minutes",
                        display_value=displayed,
                        approximation="about",
                    )
                )
                stage_parts.append(f"{stage_names[stage]}约{displayed}分钟")
        breakdown = ""
        if stage_parts:
            breakdown = "，其中" + "、".join(stage_parts)
        overlap_notice = (
            "。分期区间存在重叠，因此本次不展示各阶段分钟数。"
            if presentation.classified_totals_state == "overlap_ambiguous"
            else "。"
        )
        summary_text = (
            f"您好。{span_phrase}记录到约{observed}分钟的睡眠分期数据"
            f"{breakdown}{overlap_notice}"
        )
        alternate = (
            f"您好。本次{span_phrase}记录到的睡眠分期数据约为{observed}分钟"
            f"{breakdown}{overlap_notice}"
        )
    else:
        summary_text = "您好。本次设备记录未形成可安全展示的睡眠分期摘要。"
        alternate = "您好。本次暂时没有可安全展示的睡眠分期摘要。"
    atoms.append(
        ElderMessageAtom.create(
            semantic_type="sleep_stage_summary",
            priority=10,
            mandatory=True,
            numeric_bindings=tuple(summary_bindings),
            localized_display_values=localized_values,
            source_refs=authority_refs,
            quality_classification=shared.source.quality_state,
            risk_state=risk_state,
            safety_classification="observational",
            allowed_elder_meaning=(
                "仅说明设备记录到的分期数据时长、主体本地记录时段和可安全理解的主要分期分钟数；"
                "不把分期观测跨度表述为总睡眠时长。"
            ),
            renderings=(
                ElderAtomRendering(
                    rendering_id="sleep-stage-summary.default",
                    text=summary_text,
                ),
                ElderAtomRendering(
                    rendering_id="sleep-stage-summary.alternate",
                    text=alternate,
                ),
            ),
            fallback_rendering_id="sleep-stage-summary.default",
        )
    )

    if presentation.bed_exit_count:
        count = str(presentation.bed_exit_count)
        bed_bindings = (
            ElderNumericBinding(
                binding_key="bed_exit_count",
                value=presentation.bed_exit_count,
                unit="count",
                display_value=count,
            ),
        )
        first_time = (
            presentation.bed_exit_local_times[0]
            if presentation.bed_exit_local_times
            else None
        )
        bed_local_values = (
            {} if first_time is None else {"first_bed_exit_local_time": first_time}
        )
        time_suffix = "" if first_time is None else f"，首次在{first_time}左右"
        bed_text = f"设备在已记录时段内记录到{count}次离床{time_suffix}。"
        bed_alternate = f"已记录时段内共有{count}次离床记录{time_suffix}。"
    else:
        bed_bindings = ()
        bed_local_values = {}
        bed_text = "在设备已记录的时段内，没有记录到离床。"
        bed_alternate = "设备已记录的时段内未见离床记录。"
    atoms.append(
        ElderMessageAtom.create(
            semantic_type="bed_exit_observation",
            priority=20,
            mandatory=True,
            numeric_bindings=bed_bindings,
            localized_display_values=bed_local_values,
            source_refs=authority_refs,
            quality_classification=shared.source.quality_state,
            risk_state=risk_state,
            safety_classification="observational",
            allowed_elder_meaning="仅说明设备已记录时段内的离床观察，不外推未覆盖时段。",
            renderings=(
                ElderAtomRendering(
                    rendering_id="bed-exit.default",
                    text=bed_text,
                ),
                ElderAtomRendering(
                    rendering_id="bed-exit.alternate",
                    text=bed_alternate,
                ),
            ),
            fallback_rendering_id="bed-exit.default",
        )
    )

    presentation_limited = bool(
        presentation.invalid_interval_count
        or presentation.out_of_episode_interval_count
        or presentation.classified_totals_state == "overlap_ambiguous"
    )
    if shared.source.partial_caveat is not None or presentation_limited:
        quality_text = (
            "本次部分时段的数据不完整，因此结果仅作为日常睡眠观察参考，"
            "建议结合之后几晚的数据继续观察。"
        )
        quality_alternate = (
            "本次有部分时段的数据不完整，结果仅供日常睡眠观察；"
            "可以结合后续几晚的记录继续查看。"
        )
        atoms.append(
            ElderMessageAtom.create(
                semantic_type="quality_caveat",
                priority=30,
                mandatory=True,
                source_refs=authority_refs,
                quality_classification="partial",
                risk_state=risk_state,
                safety_classification="observational",
                allowed_elder_meaning=(
                    "保留一次 PARTIAL 或展示受限含义，并将结果限定为日常观察。"
                ),
                renderings=(
                    ElderAtomRendering(
                        rendering_id="quality-caveat.default",
                        text=quality_text,
                    ),
                    ElderAtomRendering(
                        rendering_id="quality-caveat.alternate",
                        text=quality_alternate,
                    ),
                ),
                fallback_rendering_id="quality-caveat.default",
            )
        )

    atoms.append(
        ElderMessageAtom.create(
            semantic_type="observation_boundary",
            priority=40,
            mandatory=True,
            source_refs=authority_refs,
            quality_classification="not_applicable",
            risk_state=risk_state,
            safety_classification="non_diagnostic_boundary",
            allowed_elder_meaning=(
                "说明报告由 AI 辅助整理、仅用于睡眠观察，且不构成诊断或医疗建议。"
            ),
            renderings=(
                ElderAtomRendering(
                    rendering_id="boundary.default",
                    text=(
                        "本报告由 AI 辅助整理，仅用于睡眠观察，"
                        "不构成诊断或医疗建议。"
                    ),
                ),
                ElderAtomRendering(
                    rendering_id="boundary.alternate",
                    text=(
                        "以上内容由 AI 辅助整理，仅供睡眠观察参考，"
                        "不能替代诊断或医疗建议。"
                    ),
                ),
            ),
            fallback_rendering_id="boundary.default",
        )
    )
    result = tuple(sorted(atoms, key=lambda item: (item.priority, item.atom_id)))
    validate_elder_message_atoms(shared, result)
    return result


def validate_elder_message_atoms(
    shared: SharedNightAnalysis,
    atoms: tuple[ElderMessageAtom, ...],
) -> None:
    """Bind every Elder atom to one accepted SharedNightAnalysis authority."""

    if not atoms:
        raise ValueError("Elder message atom authority cannot be empty")
    semantic_types = tuple(item.semantic_type for item in atoms)
    if len(semantic_types) != len(set(semantic_types)):
        raise ValueError("Elder message atom semantic types must be unique")
    required_types = {
        "sleep_stage_summary",
        "bed_exit_observation",
        "observation_boundary",
    }
    if not required_types.issubset(semantic_types):
        raise ValueError("Elder message atom authority omitted default meaning")
    quality_atoms = tuple(
        item for item in atoms if item.semantic_type == "quality_caveat"
    )
    if shared.source.partial_caveat is not None and len(quality_atoms) != 1:
        raise ValueError("PARTIAL shared analysis requires one Elder caveat atom")
    required_refs = {
        f"shared_analysis:sha256:{shared.shared_analysis_sha256}",
        shared.evidence.work_product_ref,
    }
    for atom in atoms:
        if not required_refs.issubset(atom.source_refs):
            raise ValueError("Elder atom is not bound to accepted Shared Analysis")
        if atom.risk_state != shared.source.risk_state:
            raise ValueError("Elder atom changed shared risk meaning")
    boundary = next(
        item for item in atoms
        if item.semantic_type == "observation_boundary"
    )
    if boundary.safety_classification != "non_diagnostic_boundary":
        raise ValueError("Elder boundary changed safety meaning")


def validate_elder_communication_draft(
    draft: CommunicationDraft,
    atoms: tuple[ElderMessageAtom, ...],
) -> None:
    """Validate the selected finite renderings without semantic guesswork."""

    atoms_by_id = {item.atom_id: item for item in atoms}
    bindings = tuple(draft.semantic_bindings)
    binding_refs = tuple(item.source_ref for item in bindings)
    if (
        draft.audience_role != "elder"
        or draft.claim_refs
        or draft.care_candidate_refs
        or draft.memory_change_candidates
        or not bindings
        or any(item.source_kind != "elder_atom" for item in bindings)
        or len(binding_refs) != len(set(binding_refs))
        or not set(binding_refs).issubset(atoms_by_id)
    ):
        raise ValueError("Elder Communication exceeds message-atom authority")
    mandatory_refs = {
        item.atom_id for item in atoms if item.mandatory
    }
    if not mandatory_refs.issubset(binding_refs):
        raise ValueError("Elder Communication omitted mandatory atoms")
    for binding in bindings:
        allowed_text = {
            item.text for item in atoms_by_id[binding.source_ref].renderings
        }
        if binding.rendered_text not in allowed_text:
            raise ValueError("Elder Communication invented a rendering")
    ordered = sorted(
        bindings,
        key=lambda item: (
            atoms_by_id[item.source_ref].priority,
            binding_refs.index(item.source_ref),
        ),
    )
    if draft.text != "\n\n".join(item.rendered_text for item in ordered):
        raise ValueError("Elder Communication text differs from bound renderings")


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
    presentation_authority_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reporting_context: ReportingContextV1 | None = None

    @model_serializer(mode="wrap")
    def serialize_projection(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        payload = dict(handler(self))
        if self.presentation_authority_sha256 is None:
            payload.pop("presentation_authority_sha256", None)
        return payload

    @model_validator(mode="after")
    def validate_projection(self) -> "RoleProjection":
        if self.presentation_authority_sha256 is not None and (
            self.role is not ReportRole.ELDER
            or self.state is not RoleProjectionState.READY
        ):
            raise ValueError(
                "presentation authority is limited to the ready Elder projection"
            )
        if self.reporting_context is not None:
            if self.reporting_context.audience != self.role.value:
                raise ValueError("projection reporting audience must match role")
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
                presentation_authority_sha256=(
                    self.presentation_authority_sha256
                ),
                reporting_context=self.reporting_context,
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
    projection_policy_version: str = "shared_role_projection.v3",
    renderer_version: str = ZH_CN_ROLE_RENDERER_VERSION,
) -> dict[str, Any]:
    """Canonical deterministic projection pins, independent of analysis."""

    return {
        "schema_version": "role_projection_runtime_manifest.v1",
        "projection_schema": ROLE_PROJECTION_SCHEMA_VERSION,
        "projection_policy_version": projection_policy_version,
        "projector_version": "build_shared_role_projections.v3",
        "locale": "zh-CN",
        "renderer_version": renderer_version,
        "elder_presentation_policy_version": (
            "bounded_semantic_elder_atoms.v1"
        ),
        "elder_numeric_rendering_policy": (
            "runtime_defined_display_only.v1"
        ),
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


_ZH_CN_METRIC_LABELS = {
    "bed_exit_count": "离床次数",
    "sleep_stage.light_minutes": "浅睡",
    "sleep_stage.deep_minutes": "深睡",
    "sleep_stage.rem_minutes": "REM 睡眠",
    "sleep_stage.awake_minutes": "清醒",
    "heart_rate_mean": "平均心率",
    "respiratory_rate_mean": "平均呼吸率",
    "data_coverage_ratio": "数据覆盖率",
}
_ZH_CN_UNIT_LABELS = {
    "minutes": "分钟",
    "count": "次",
    "bpm": "bpm",
    "breaths_per_minute": "次/分钟",
    "ratio": "",
}
_ZH_CN_QUALITY_LABELS = {
    "good": "完整",
    "partial": "部分完整",
}


def _report_value_text(fact: ReportSemanticFact) -> str:
    value = fact.value
    if fact.unit == "ratio" and isinstance(value, (int, float)):
        return f"{round(float(value) * 100)}%"
    if isinstance(value, float):
        rendered = str(int(value)) if value.is_integer() else f"{value:.1f}"
    else:
        rendered = str(value)
    return rendered + _ZH_CN_UNIT_LABELS.get(fact.unit or "", fact.unit or "")


def _localized_report_span(context: ReportingContextV1) -> str:
    zone = ZoneInfo(context.timezone_name)
    start = context.authoritative_start_at_utc.astimezone(zone)
    end = context.authoritative_end_at_utc.astimezone(zone)
    if start.date() == end.date():
        return f"{start:%m月%d日 %H:%M}至{end:%H:%M}"
    return f"{start:%m月%d日 %H:%M}至{end:%m月%d日 %H:%M}"


def _localized_sleep_stage_span(
    by_metric: Mapping[str, ReportSemanticFact],
    context: ReportingContextV1 | None,
) -> str | None:
    if context is None:
        return None
    start_fact = by_metric.get("sleep_stage_coverage_start_at")
    end_fact = by_metric.get("sleep_stage_coverage_end_at")
    if start_fact is None or end_fact is None:
        return None
    start = _report_datetime(start_fact.value)
    end = _report_datetime(end_fact.value)
    if start is None or end is None:
        return None
    zone = ZoneInfo(context.timezone_name)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    if local_start.date() == local_end.date():
        return f"{local_start:%m月%d日 %H:%M}至{local_end:%H:%M}"
    return (
        f"{local_start:%m月%d日 %H:%M}至"
        f"{local_end:%m月%d日 %H:%M}"
    )


def _render_zh_cn_role(
    *,
    role: ReportRole,
    shared: SharedNightAnalysis,
    caveats: tuple[str, ...],
) -> str:
    """Render only structured authority; accepted free-form prose is ignored."""

    context = shared.source.reporting_context
    local_date = shared.source.wake_date
    span = None if context is None else _localized_report_span(context)
    direct = tuple(
        item for item in shared.semantic_facts
        if item.fact_kind == "direct_metric" and item.value is not None
    )
    by_metric = {item.metric_id: item for item in direct}
    stage_span = _localized_sleep_stage_span(by_metric, context)
    stage_duration = by_metric.get("sleep_window_minutes")
    care_pending = any(
        item.fact_kind == "care_candidate" and item.value is True
        for item in shared.semantic_facts
    )
    quality_text = "数据完整" if shared.source.quality_state == "good" else "数据部分完整"
    risk_text = {
        "no_reviewed_signal": "未见经确认的风险信号",
        "unknown": "风险状态暂不确定",
        "no_active_alert": "未见当前告警",
    }.get(shared.source.risk_state, "风险状态需结合证据审阅")

    if role is ReportRole.ELDER:
        lines = [f"您好。我们已为您整理{local_date:%m月%d日}的睡眠观察。"]
        if span is not None:
            lines.append(f"设备观测窗口为{span}。")
        if stage_span is not None and stage_duration is not None:
            lines.append(
                f"睡眠分期覆盖为{stage_span}，"
                f"共约{_report_value_text(stage_duration)}。"
            )
        exits = by_metric.get("bed_exit_count")
        if exits is not None:
            lines.append(f"设备记录到离床{_report_value_text(exits)}。")
        if shared.source.partial_caveat:
            lines.append(shared.source.partial_caveat)
        lines.append("以上内容仅用于睡眠观察，不构成诊断或医疗建议。")
        return "\n".join(lines)

    if role is ReportRole.FAMILY:
        lines = [f"{local_date:%Y年%m月%d日}家属睡眠照护摘要"]
        if span is not None:
            lines.append(f"观测窗口：{span}。")
        if stage_span is not None and stage_duration is not None:
            lines.append(
                f"睡眠分期覆盖：{stage_span}，"
                f"共约{_report_value_text(stage_duration)}。"
            )
        lines.append(f"整体状态：{quality_text}；{risk_text}。")
        for metric_id in (
            "bed_exit_count",
            "heart_rate_mean",
            "respiratory_rate_mean",
        ):
            fact = by_metric.get(metric_id)
            if fact is not None:
                lines.append(
                    f"{_ZH_CN_METRIC_LABELS[metric_id]}：{_report_value_text(fact)}。"
                )
        lines.append(
            "照护建议：有一项建议待确认。"
            if care_pending
            else "照护建议：继续按现有方式观察后续几晚。"
        )
        if shared.source.partial_caveat:
            lines.append(f"不确定性：{shared.source.partial_caveat}")
        lines.append("本摘要仅用于睡眠健康观察，不构成诊断或医疗建议。")
        return "\n".join(lines)

    lines = [f"{local_date:%Y年%m月%d日}睡眠观察证据摘要"]
    if span is not None and context is not None:
        lines.extend(
            (
                f"权威观测窗口：{span}。",
                f"时区：{context.timezone_name}；本地睡眠日期：{local_date.isoformat()}。",
            )
        )
    if stage_span is not None and stage_duration is not None:
        lines.append(
            f"睡眠分期覆盖：{stage_span}，"
            f"共约{_report_value_text(stage_duration)}。"
        )
    lines.append(f"数据质量：{quality_text}；{risk_text}。")
    for fact in direct:
        label = _ZH_CN_METRIC_LABELS.get(fact.metric_id)
        if label is None:
            continue
        lines.append(
            f"{label}：{_report_value_text(fact)}；"
            "质量限定："
            f"{_ZH_CN_QUALITY_LABELS.get(fact.quality_qualifier or '', '未标注')}。"
        )
    authority_count = len(
        {
            ref
            for fact in shared.semantic_facts
            for ref in fact.source_refs
        }
    )
    lines.append(f"证据来源：{authority_count}项受治理来源引用。")
    if care_pending:
        lines.append("照护候选：存在待确认候选，尚未执行。")
    if shared.source.partial_caveat:
        lines.append(f"数据限制：{shared.source.partial_caveat}")
    lines.extend(f"观察边界：{item}" for item in caveats)
    lines.append("不得据此形成未经支持的诊断结论。")
    return "\n".join(lines)


def build_shared_role_projections(
    shared: SharedNightAnalysis,
    *,
    elder_message_atoms: tuple[ElderMessageAtom, ...] = (),
    renderer_version: str = ZH_CN_ROLE_RENDERER_VERSION,
) -> tuple[RoleProjection, RoleProjection, RoleProjection]:
    """Build all three deterministic views without invoking a model."""

    if elder_message_atoms:
        validate_elder_message_atoms(shared, elder_message_atoms)

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
            if role is ReportRole.ELDER and elder_message_atoms:
                text = deterministic_elder_fallback(elder_message_atoms)
            else:
                text = _render_zh_cn_role(
                    role=role,
                    shared=shared,
                    caveats=role_caveats,
                )
        presentation_authority_sha256 = (
            elder_presentation_authority_sha256(elder_message_atoms)
            if role is ReportRole.ELDER and elder_message_atoms
            else None
        )
        reporting_context = (
            None
            if shared.source.reporting_context is None
            else shared.source.reporting_context.for_audience(
                role.value,
                renderer_version=renderer_version,
            )
        )
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
            presentation_authority_sha256=(
                presentation_authority_sha256
            ),
            reporting_context=reporting_context,
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
                presentation_authority_sha256=(
                    presentation_authority_sha256
                ),
                reporting_context=reporting_context,
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
    message_atoms: tuple[ElderMessageAtom, ...] = Field(min_length=1)
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
        message_atoms: tuple[ElderMessageAtom, ...],
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
            message_atoms=message_atoms,
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
        if (
            projection.presentation_authority_sha256 is None
            or elder_presentation_authority_sha256(self.message_atoms)
            != projection.presentation_authority_sha256
        ):
            raise ValueError(
                "elder narrative atoms differ from projection authority"
            )
        validate_elder_message_atoms(self.shared_analysis, self.message_atoms)
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
    "REPORTING_CONTEXT_SCHEMA_VERSION",
    "REPORT_SEMANTIC_FACT_SCHEMA_VERSION",
    "ROLE_PROJECTION_SCHEMA_VERSION",
    "SHARED_ANALYSIS_SOURCE_SCHEMA_VERSION",
    "SHARED_NIGHT_ANALYSIS_SCHEMA_VERSION",
    "ElderNarrative",
    "ElderNarrativeRequest",
    "ElderNarrativeState",
    "ElderAtomRendering",
    "ElderMessageAtom",
    "ElderNumericBinding",
    "ReportRole",
    "ReportingContextV1",
    "ReportSemanticFact",
    "RoleProjection",
    "RoleProjectionState",
    "RoleReportArtifact",
    "RoleReportBundle",
    "SharedAnalysisRunRequest",
    "SharedAnalysisSourceV1",
    "SharedNightAnalysis",
    "build_elder_narrative_runtime_manifest",
    "build_elder_message_atoms",
    "build_role_projection_runtime_manifest",
    "build_role_report_templates",
    "build_report_semantic_facts",
    "build_safe_model_pin",
    "build_shared_analysis_runtime_manifest",
    "build_shared_role_projections",
    "deterministic_elder_fallback",
    "elder_presentation_authority_sha256",
    "validate_elder_communication_draft",
    "validate_elder_message_atoms",
    "role_projection_identity_sha256",
    "ZH_CN_ROLE_RENDERER_VERSION",
]
