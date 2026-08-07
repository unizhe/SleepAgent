from __future__ import annotations

from sleepagent.radar_agent.schemas import AgentResult, RadarAgentName

from .contracts import SkillSpec


def _skill(
    skill_id: str,
    agent: RadarAgentName | list[RadarAgentName],
    scope: str,
    rules: list[str],
    forbidden: list[str],
) -> SkillSpec:
    return SkillSpec(
        skill_id=skill_id,
        version="1.0.0",
        applicable_agents=agent if isinstance(agent, list) else [agent],
        scope=scope,
        output_schema=AgentResult.model_json_schema(),
        rules=rules,
        forbidden_actions=forbidden,
    )


ORCHESTRATOR_SKILL = _skill(
    "OrchestratorSkill",
    RadarAgentName.ORCHESTRATOR,
    "单任务内的上下文裁剪、A2A 路由、冲突裁决和最终发布控制。",
    ["按工作流顺序推进", "数据质量、证据账本和安全边界优先", "高风险动作进入确认队列"],
    ["承担跨任务生命周期", "替子 Agent 编造证据"],
)
RADAR_DATA_SKILL = _skill(
    "RadarDataSkill",
    RadarAgentName.RADAR_DATA,
    "雷达 canonical 数据读取、规范化、设备状态和质量门控。",
    ["业务链只输出 canonical schema", "质量不足时 fail-soft", "总是披露覆盖率和缺失"],
    ["把缺失解释成健康结论", "向业务输出原始厂商 payload"],
)
TREND_ANALYSIS_SKILL = _skill(
    "TrendAnalysisSkill",
    RadarAgentName.TREND,
    "基于可解释夜间摘要计算 7/30/90 天趋势和个体基线差异。",
    ["只使用通过质量门控的指标", "样本不足时明确不确定", "每个趋势 claim 引用证据"],
    ["输出诊断", "用单晚数据冒充长期趋势"],
)
RISK_SIGNAL_SKILL = _skill(
    "RiskSignalSkill",
    [RadarAgentName.RISK_SIGNAL, RadarAgentName.ALERT_CARE],
    "把规则证据分级为风险线索，并把告警/照护输出限制为需确认的候选动作。",
    ["风险由规则决定而非 LLM", "急症文本覆盖睡眠解释", "质量差时不强行升级"],
    ["临床诊断", "精确 AHI 或 PSG 级结论", "药物建议"],
)
RAG_GROUNDING_SKILL = _skill(
    "RAGGroundingSkill",
    RadarAgentName.RAG,
    "从 reviewed seed knowledge 路由角色适配片段并保留版本、来源和 caveat。",
    ["只检索已审阅条目", "引用 chunk_id", "医学安全来源优先"],
    ["自由网络检索", "引用未审阅医学内容"],
)
ROLE_REPORT_SKILL = _skill(
    "RoleReportSkill",
    RadarAgentName.REPORT,
    "从同一 Evidence Ledger 生成老人、家属、医生三角色表达。",
    ["三版共享同一 claim 集", "医生版保留完整质量与 caveat", "LLM 失败使用模板"],
    ["新增或修改事实 claim", "确定诊断", "省略证据边界"],
)
DIALOGUE_SKILL = _skill(
    "DialogueSkill",
    RadarAgentName.DIALOGUE,
    "按角色解释现有报告与证据，并从版本化题库提出微问卷候选。",
    ["回答必须引用 Evidence Ledger 或 RAG", "急症边界优先", "每轮最多 1-3 个题库问题"],
    ["直接改变事实源", "临场编造医学筛查问题", "执行外部动作"],
)
MEMORY_SKILL = _skill(
    "MemorySkill",
    RadarAgentName.MEMORY,
    "生成最小化、可追溯、待确认的趋势/偏好/照护事件记忆候选。",
    ["只生成候选", "敏感内容最小化", "长期写入需要确认"],
    ["直接写长期记忆", "保存原始雷达流", "保存未经同意的敏感对话"],
)

SKILL_REGISTRY = {
    skill.skill_id: skill
    for skill in (
        ORCHESTRATOR_SKILL,
        RADAR_DATA_SKILL,
        TREND_ANALYSIS_SKILL,
        RISK_SIGNAL_SKILL,
        RAG_GROUNDING_SKILL,
        ROLE_REPORT_SKILL,
        DIALOGUE_SKILL,
        MEMORY_SKILL,
    )
}
SKILL_BY_AGENT = {
    agent: skill
    for skill in SKILL_REGISTRY.values()
    for agent in skill.applicable_agents
}


__all__ = [
    "DIALOGUE_SKILL",
    "MEMORY_SKILL",
    "ORCHESTRATOR_SKILL",
    "RADAR_DATA_SKILL",
    "RAG_GROUNDING_SKILL",
    "RISK_SIGNAL_SKILL",
    "ROLE_REPORT_SKILL",
    "SKILL_REGISTRY",
    "SKILL_BY_AGENT",
    "TREND_ANALYSIS_SKILL",
]
