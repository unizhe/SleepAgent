from __future__ import annotations

import json

import pytest

from sleepagent.radar_agent.agents import ContextPacket, TaskContext
from sleepagent.radar_agent.prompts import (
    GLOBAL_SAFETY_POLICY_PROMPT,
    TASK_CONTEXT_PACKET_PROMPT,
    PromptLayer,
    build_prompt_bundle,
    skill_prompt,
)
from sleepagent.radar_agent.skills import SKILL_REGISTRY
from sleepagent.radar_agent.llm import build_cloud_context_envelope


@pytest.mark.parametrize("skill_id", sorted(SKILL_REGISTRY))
def test_each_skill_has_version_scope_output_schema_and_rules(skill_id: str) -> None:
    skill = SKILL_REGISTRY[skill_id]

    assert skill.version == "1.0.0"
    assert skill.scope
    assert skill.applicable_agents
    assert skill.output_schema["title"] == "AgentResult"
    assert skill.rules


@pytest.mark.parametrize("skill_id", sorted(SKILL_REGISTRY))
def test_each_agent_skill_prompt_is_versioned_scoped_and_schema_bound(skill_id: str) -> None:
    skill = SKILL_REGISTRY[skill_id]
    prompt = skill_prompt(skill)

    assert prompt.prompt_id == f"agent-skill:{skill_id}"
    assert prompt.version == skill.version
    assert prompt.scope == skill.scope
    assert prompt.layer == PromptLayer.AGENT_SKILL_PROMPT
    assert prompt.output_schema == skill.output_schema
    assert len(prompt.template) < 1000


def test_global_and_task_prompts_have_independent_contracts() -> None:
    assert GLOBAL_SAFETY_POLICY_PROMPT.version == "1.0.0"
    assert GLOBAL_SAFETY_POLICY_PROMPT.scope
    assert GLOBAL_SAFETY_POLICY_PROMPT.output_schema
    assert GLOBAL_SAFETY_POLICY_PROMPT.layer == PromptLayer.GLOBAL_SAFETY_POLICY
    assert TASK_CONTEXT_PACKET_PROMPT.version == "1.0.0"
    assert TASK_CONTEXT_PACKET_PROMPT.scope
    assert TASK_CONTEXT_PACKET_PROMPT.output_schema
    assert TASK_CONTEXT_PACKET_PROMPT.layer == PromptLayer.TASK_CONTEXT_PACKET


def test_prompt_bundle_uses_exactly_three_layers_without_giant_system_prompt() -> None:
    context = ContextPacket(
        context_packet_id="context-task-prompt",
        task_context=TaskContext(task_id="task-prompt", trace_id="trace-prompt"),
        memory_snippets=["只提供必要的偏好摘要"],
    )
    bundle = build_prompt_bundle(SKILL_REGISTRY["DialogueSkill"], context)

    assert [message["role"] for message in bundle.messages] == ["system", "developer", "user"]
    assert bundle.messages[0]["content"] == GLOBAL_SAFETY_POLICY_PROMPT.template
    assert "task-prompt" not in bundle.messages[0]["content"]
    assert "DialogueSkill" in bundle.messages[1]["content"]
    assert json.dumps(
        build_cloud_context_envelope(context), ensure_ascii=False
    ) in bundle.messages[2]["content"]
    assert "vital_snapshots" not in bundle.messages[2]["content"]
    assert "source_raw_event_ids" not in bundle.messages[2]["content"]
    assert len(bundle.messages[0]["content"]) < 1000
