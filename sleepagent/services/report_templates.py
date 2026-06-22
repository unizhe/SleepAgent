from sleepagent.schemas import (
    MockSleepReport,
    RetrievedReportKnowledgeChunk,
    ReportSummary,
    RiskLevel,
    SleepAnalysisResult,
)
from sleepagent.services.report_retrievers import (
    ReportKnowledgeRetriever,
    ReportRetrieverConfig,
    retrieve_report_context,
)


MEDICAL_DISCLAIMER = (
    "本报告由 SleepAgent MVP mock 数据生成，仅用于系统联调和科研原型展示，"
    "不能替代医生诊断、治疗建议或急救判断。若出现明显憋醒、胸闷、严重打鼾、"
    "白天嗜睡或呼吸困难，请及时咨询专业医生。"
)
REAL_MEDICAL_DISCLAIMER = (
    "本报告基于本地 SHHS PSG 数据的辅助分析结果，仅用于睡眠健康研究和风险提示，"
    "不能替代医生诊断、治疗建议或急救判断。若出现胸痛、严重呼吸困难、意识异常"
    "等情况，请及时就医。"
)


def generate_sleep_report(
    analysis: SleepAnalysisResult,
    *,
    retriever: ReportKnowledgeRetriever | None = None,
    retriever_config: ReportRetrieverConfig | None = None,
) -> MockSleepReport:
    """Generate a source-aware template report for the primary analysis flow."""
    return _generate_template_sleep_report(
        analysis,
        is_mock=analysis.metadata.source_dataset == "mock",
        retriever=retriever,
        retriever_config=retriever_config,
    )


def generate_mock_sleep_report(
    analysis: SleepAnalysisResult,
    *,
    retriever: ReportKnowledgeRetriever | None = None,
    retriever_config: ReportRetrieverConfig | None = None,
) -> MockSleepReport:
    """Generate the explicit legacy mock report used by mock-only endpoints."""
    return _generate_template_sleep_report(
        analysis,
        is_mock=True,
        retriever=retriever,
        retriever_config=retriever_config,
    )


def _generate_template_sleep_report(
    analysis: SleepAnalysisResult,
    *,
    is_mock: bool,
    retriever: ReportKnowledgeRetriever | None,
    retriever_config: ReportRetrieverConfig | None,
) -> MockSleepReport:
    summary = _build_report_summary(analysis)
    retrieved_knowledge = retrieve_report_context(
        _build_report_knowledge_query(summary),
        top_k=3,
        retriever=retriever,
        config=retriever_config,
    )
    return MockSleepReport(
        summary=summary,
        elder_report=_build_elder_report(summary, retrieved_knowledge, is_mock=is_mock),
        professional_report=_build_professional_report(
            summary,
            retrieved_knowledge,
            is_mock=is_mock,
        ),
        care_suggestions=_build_care_suggestions(summary, retrieved_knowledge),
        medical_disclaimer=(MEDICAL_DISCLAIMER if is_mock else REAL_MEDICAL_DISCLAIMER),
        generated_at=analysis.generated_at,
    )


def report_disclaimer_for_analysis(analysis: SleepAnalysisResult) -> str:
    if analysis.metadata.source_dataset == "mock":
        return MEDICAL_DISCLAIMER
    return REAL_MEDICAL_DISCLAIMER


def _build_report_summary(analysis: SleepAnalysisResult) -> ReportSummary:
    return ReportSummary(
        record_id=analysis.metadata.record_id,
        subject_id=analysis.metadata.patient.subject_id,
        risk_level=analysis.risk_level,
        total_recording_minutes=analysis.sleep_summary.total_recording_minutes,
        total_sleep_minutes=analysis.sleep_summary.total_sleep_time_minutes,
        sleep_efficiency_percent=analysis.sleep_summary.sleep_efficiency_percent,
        ahi=analysis.respiratory_summary.ahi,
        hypopnea_count=analysis.respiratory_summary.hypopnea_count,
        suspected_apnea_count=analysis.respiratory_summary.suspected_apnea_count,
        mean_respiratory_rate_bpm=analysis.respiratory_summary.mean_respiratory_rate_bpm,
    )


def _build_elder_report(
    summary: ReportSummary,
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
    *,
    is_mock: bool,
) -> str:
    risk_text = _risk_text(summary.risk_level)
    introduction = (
        "这是一份模拟睡眠报告。"
        if is_mock
        else "这是一份基于本地 SHHS PSG 记录的辅助睡眠报告。"
    )
    report = (
        f"{introduction}昨晚记录了约 {summary.total_recording_minutes:.0f} 分钟，"
        f"其中大约 {summary.total_sleep_minutes:.0f} 分钟处于睡眠状态，"
        f"睡眠效率约为 {summary.sleep_efficiency_percent:.1f}%。"
        f"系统估计每小时疑似呼吸异常次数 AHI 约为 {summary.ahi:.1f}，"
        f"整体风险提示为{risk_text}。"
        "请把它看作一个提醒工具，不要因为单次结果过度紧张。"
    )
    context = _elder_context_sentence(retrieved_knowledge)
    if context:
        report += context
    return report


def _build_professional_report(
    summary: ReportSummary,
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
    *,
    is_mock: bool,
) -> str:
    mean_rate = (
        f"{summary.mean_respiratory_rate_bpm:.1f} 次/分"
        if summary.mean_respiratory_rate_bpm is not None
        else "暂无"
    )
    report_prefix = (
        "SleepAgent MVP mock report: "
        if is_mock
        else "SleepAgent SHHS PSG analysis report: "
    )
    source_note = (
        "Current result is generated from synthetic mock data and should be replaced "
        "by validated PSG analysis before clinical use."
        if is_mock
        else "Current result is derived from local SHHS EDF/XML data with YASA staging; "
        "it remains an assistive research output requiring scorer and clinician review."
    )
    report = report_prefix + (
        f"record_id={summary.record_id}, subject_id={summary.subject_id}. "
        f"Total recording time={summary.total_recording_minutes:.1f} min, "
        f"total sleep time={summary.total_sleep_minutes:.1f} min, "
        f"sleep efficiency={summary.sleep_efficiency_percent:.1f}%. "
        f"AHI={summary.ahi:.2f}, hypopnea_count={summary.hypopnea_count}, "
        f"suspected_apnea_count={summary.suspected_apnea_count}, "
        f"mean_respiratory_rate={mean_rate}, risk_level={summary.risk_level.value}. "
        f"{source_note}"
    )
    context = _professional_context_sentence(retrieved_knowledge)
    if context:
        report += context
    return report


def _build_care_suggestions(
    summary: ReportSummary,
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
) -> list[str]:
    suggestions = [
        "保持规律作息，避免睡前饮酒和过度进食。",
        "如果家属观察到频繁打鼾、憋醒或白天明显困倦，建议记录发生频率。",
    ]
    if summary.risk_level in {RiskLevel.MODERATE, RiskLevel.HIGH}:
        suggestions.append("建议携带完整睡眠记录或 PSG 报告，咨询睡眠医学或呼吸科医生。")
    if summary.risk_level == RiskLevel.HIGH:
        suggestions.append("若同时出现胸闷、呼吸困难、意识异常等情况，应及时就医。")
    if _has_retrieved_chunk(retrieved_knowledge, "urgent-symptoms-safety"):
        suggestions.append("若出现胸痛、严重呼吸困难或意识异常，请优先及时就医或急诊评估。")
    return suggestions


def _build_report_knowledge_query(summary: ReportSummary) -> list[str]:
    query_terms = [
        "sleep_efficiency",
        "sleep_summary",
        "ahi",
        "hypopnea",
        "suspected_apnea",
        "respiratory_events",
    ]
    if summary.risk_level in {RiskLevel.MODERATE, RiskLevel.HIGH}:
        query_terms.extend(["medical_safety", "urgent_care", "high_risk"])
    return query_terms


def _elder_context_sentence(
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
) -> str:
    if _has_retrieved_chunk(retrieved_knowledge, "ahi-basic"):
        return (
            " AHI 只是帮助判断风险的线索，不能单独当作诊断；"
            "如果症状明显或指标持续偏高，建议让医生结合完整 PSG 报告判断。"
        )
    return ""


def _professional_context_sentence(
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
) -> str:
    if not retrieved_knowledge:
        return ""

    references = ", ".join(item.chunk.chunk_id for item in retrieved_knowledge)
    safety_notes = [
        note
        for item in retrieved_knowledge
        for note in item.chunk.safety_notes
    ]
    safety_text = f" Safety notes: {'; '.join(safety_notes)}." if safety_notes else ""
    return f" Retrieved local context chunks: {references}.{safety_text}"


def _has_retrieved_chunk(
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
    chunk_id: str,
) -> bool:
    return any(item.chunk.chunk_id == chunk_id for item in retrieved_knowledge)


def _risk_text(risk_level: RiskLevel) -> str:
    if risk_level == RiskLevel.LOW:
        return "低风险"
    if risk_level == RiskLevel.MODERATE:
        return "中等风险"
    return "较高风险"
