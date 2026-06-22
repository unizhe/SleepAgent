from sleepagent.schemas import (
    DialogueContext,
    DialogueTurn,
    MockSleepReport,
    RiskLevel,
    SleepAnalysisResult,
)
from sleepagent.services.memory import build_dialogue_context_from_memory
from sleepagent.services.memory_repository import MemoryRepository
from sleepagent.services.safety import check_dialogue_safety


URGENT_SYMPTOM_KEYWORDS = (
    "胸痛",
    "严重呼吸困难",
    "呼吸困难",
    "意识异常",
    "喘不上气",
    "晕厥",
    "chest pain",
    "severe breathing difficulty",
    "shortness of breath",
    "loss of consciousness",
)
AHI_KEYWORDS = ("ahi", "呼吸暂停", "低通气", "憋醒", "打鼾", "呼吸")
SUGGESTION_KEYWORDS = ("建议", "怎么办", "改善", "注意", "照护", "care", "advice")


class DialogueAgent:
    """Rule-based Stage 8 dialogue agent for report-grounded Q&A."""

    def __init__(
        self,
        *,
        memory_repository: MemoryRepository | None = None,
    ) -> None:
        self.memory_repository = memory_repository

    def run(
        self,
        *,
        user_question: str,
        analysis: SleepAnalysisResult,
        report: MockSleepReport,
        dialogue_context: DialogueContext | None = None,
    ) -> DialogueTurn:
        resolved_dialogue_context = dialogue_context or self._load_memory_context(
            analysis.metadata.patient.subject_id
        )
        question = user_question.strip()
        response, safety_flags = _build_response(
            question,
            analysis,
            report,
            resolved_dialogue_context,
        )
        safety_result = check_dialogue_safety(response)
        return DialogueTurn(
            user_question=question,
            assistant_response=response,
            safety_flags=_dedupe([*safety_flags, *safety_result.safety_flags]),
            referenced_record_id=analysis.metadata.record_id,
            context_used=_has_dialogue_context(resolved_dialogue_context),
        )

    def _load_memory_context(self, subject_id: str) -> DialogueContext | None:
        if self.memory_repository is None:
            return None
        memory_summary = self.memory_repository.get_latest_memory_summary(subject_id)
        if memory_summary is None:
            return None
        return build_dialogue_context_from_memory(memory_summary)


def _build_response(
    question: str,
    analysis: SleepAnalysisResult,
    report: MockSleepReport,
    dialogue_context: DialogueContext | None,
) -> tuple[str, list[str]]:
    normalized = question.lower()
    if _has_any(normalized, URGENT_SYMPTOM_KEYWORDS):
        return (
            "如果现在出现胸痛、严重呼吸困难、意识异常或类似急症表现，"
            "请优先及时就医或急诊评估。SleepAgent 只能解释睡眠报告，"
            "不能判断急症原因，也不能替代医生处理。",
            ["urgent_symptom_safety_boundary"],
        )

    summary = report.summary
    context_sentence = _dialogue_context_sentence(dialogue_context)
    if _has_any(normalized, AHI_KEYWORDS):
        return (
            f"这次记录的 AHI 约为 {summary.ahi:.1f}，系统风险提示为"
            f"{_risk_text(analysis.risk_level)}。AHI 表示每小时疑似呼吸异常的次数，"
            "它是风险线索，不等同于诊断；如果打鼾、憋醒或白天困倦明显，"
            f"建议带完整 PSG 或睡眠记录咨询睡眠医学或呼吸科医生。{context_sentence}",
            [],
        )

    if _has_any(normalized, SUGGESTION_KEYWORDS):
        suggestions = "；".join(report.care_suggestions[:3])
        return (
            f"可以先按报告里的建议观察：{suggestions}。这些建议用于辅助照护，"
            f"如果症状持续或风险升高，应由医生结合完整检查判断。{context_sentence}",
            [],
        )

    return (
        f"这份报告对应记录 {summary.record_id}，总睡眠约 "
        f"{summary.total_sleep_minutes:.0f} 分钟，睡眠效率约 "
        f"{summary.sleep_efficiency_percent:.1f}%，风险提示为"
        f"{_risk_text(analysis.risk_level)}。我可以继续解释 AHI、呼吸事件、"
        f"睡眠效率或报告中的照护建议。{context_sentence}",
        [],
    )


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _risk_text(risk_level: RiskLevel) -> str:
    if risk_level == RiskLevel.LOW:
        return "低风险"
    if risk_level == RiskLevel.MODERATE:
        return "中等风险"
    return "较高风险"


def _has_dialogue_context(dialogue_context: DialogueContext | None) -> bool:
    if dialogue_context is None:
        return False
    return bool(
        dialogue_context.history_summary
        or dialogue_context.user_preferences
        or dialogue_context.recent_questions
    )


def _dialogue_context_sentence(dialogue_context: DialogueContext | None) -> str:
    if not _has_dialogue_context(dialogue_context):
        return ""

    assert dialogue_context is not None
    fragments: list[str] = []
    if dialogue_context.history_summary:
        fragments.append(f"历史摘要提示：{dialogue_context.history_summary}")
    if dialogue_context.user_preferences:
        preferences = "、".join(dialogue_context.user_preferences[:3])
        fragments.append(f"已参考偏好：{preferences}")
    if dialogue_context.recent_questions:
        recent_question = dialogue_context.recent_questions[-1]
        fragments.append(f"最近关注：{recent_question}")
    return " " + "；".join(fragments) + "。"


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped
