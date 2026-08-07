from __future__ import annotations

import json

from sleepagent.radar_agent.schemas import AgentResult, ContextPacket
from sleepagent.radar_agent.llm import build_cloud_context_envelope
from sleepagent.radar_agent.skills import SkillSpec

from .contracts import PromptBundle, PromptLayer, PromptSpec


GLOBAL_SAFETY_POLICY_PROMPT = PromptSpec(
    prompt_id="global-safety-policy",
    version="1.0.0",
    layer=PromptLayer.GLOBAL_SAFETY_POLICY,
    scope="所有 radar-first Agent 的非诊断、证据、隐私和确认底线。",
    output_schema=AgentResult.model_json_schema(),
    template=(
        "你是睡眠健康观察与照护协同系统的一部分。不得诊断、给药或把雷达等同 PSG；"
        "只基于所给证据，缺失时承认不确定；急症线索优先提示线下医疗/急救评估；"
        "外发、医生材料和长期记忆遵守确认要求。"
    ),
)

TASK_CONTEXT_PACKET_PROMPT = PromptSpec(
    prompt_id="task-context-packet",
    version="1.0.0",
    layer=PromptLayer.TASK_CONTEXT_PACKET,
    scope="单次任务的最小事实包；不得扩展为全量用户档案。",
    output_schema=ContextPacket.model_json_schema(),
    template="仅使用下列结构化 ContextPacket 完成当前任务：{context_packet}",
)


def skill_prompt(skill: SkillSpec) -> PromptSpec:
    return PromptSpec(
        prompt_id=f"agent-skill:{skill.skill_id}",
        version=skill.version,
        layer=PromptLayer.AGENT_SKILL_PROMPT,
        scope=skill.scope,
        output_schema=skill.output_schema,
        template=(
            f"启用 {skill.skill_id}。规则：" + "；".join(skill.rules) +
            "。禁止：" + "；".join(skill.forbidden_actions) +
            "。输出必须符合声明的结构化 schema。"
        ),
    )


def build_prompt_bundle(skill: SkillSpec, context: ContextPacket) -> PromptBundle:
    agent_prompt = skill_prompt(skill)
    context_text = TASK_CONTEXT_PACKET_PROMPT.template.format(
        context_packet=json.dumps(
            build_cloud_context_envelope(context), ensure_ascii=False
        )
    )
    return PromptBundle(
        global_policy=GLOBAL_SAFETY_POLICY_PROMPT,
        agent_skill=agent_prompt,
        task_context=TASK_CONTEXT_PACKET_PROMPT,
        messages=[
            {"role": "system", "content": GLOBAL_SAFETY_POLICY_PROMPT.template},
            {"role": "developer", "content": agent_prompt.template},
            {"role": "user", "content": context_text},
        ],
    )


__all__ = [
    "GLOBAL_SAFETY_POLICY_PROMPT",
    "TASK_CONTEXT_PACKET_PROMPT",
    "build_prompt_bundle",
    "skill_prompt",
]
