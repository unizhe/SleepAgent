from __future__ import annotations

from typing import Literal

from sleepagent.radar_agent.llm import ModelInvocationMetadata, ModelRouter
from sleepagent.radar_agent.schemas import RadarAgentSchema


class QuestionnaireToneChoice(RadarAgentSchema):
    tone: Literal["warm", "neutral", "concise"]


class ModelRouterToneRewriter:
    """Lets the LLM choose tone only; the reviewed question body stays immutable."""

    def __init__(self, model_router: ModelRouter) -> None:
        self.model_router = model_router
        self.last_status: dict[str, str | bool | None] | None = None

    def rewrite_question(self, *, text: str, role: str) -> str:
        result = self.model_router.generate_structured(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "只能从 warm/neutral/concise 中选择语气。不得改写、补充或"
                        "编造问卷题干，不得新增医学筛查问题。"
                    ),
                },
                {"role": "user", "content": f"为 {role} 角色选择问卷语气。"},
            ],
            schema=QuestionnaireToneChoice,
            metadata=ModelInvocationMetadata(
                model_id=self.model_router.client.config.model_id,
                prompt_version="questionnaire-tone-choice.v1",
            ),
            fallback=lambda: QuestionnaireToneChoice(tone="neutral"),
            fallback_summary="questionnaire_original_tone",
        )
        self.last_status = {
            "status": result.metadata.output_schema_status,
            "fallback_used": result.metadata.fallback_used,
            "fallback_reason": result.metadata.fallback_reason,
        }
        prefix = {
            "warm": "想请您确认一下：",
            "neutral": "",
            "concise": "请确认：",
        }[result.value.tone]
        return prefix + text


__all__ = ["ModelRouterToneRewriter", "QuestionnaireToneChoice"]
