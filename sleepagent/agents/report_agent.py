from sleepagent.schemas import MockSleepReport, SleepAnalysisResult
from sleepagent.services import (
    DeepSeekChatClient,
    DeepSeekReportGeneratorConfig,
    ReportKnowledgeRetriever,
    ReportRetrieverConfig,
    generate_sleep_report,
    generate_sleep_report_with_deepseek_fallback,
)


class ReportAgent:
    """Stage 8 report agent using the Stage 7 report/RAG/DeepSeek boundary."""

    def __init__(
        self,
        *,
        deepseek_client: DeepSeekChatClient | None = None,
        deepseek_config: DeepSeekReportGeneratorConfig | None = None,
        retriever: ReportKnowledgeRetriever | None = None,
        retriever_config: ReportRetrieverConfig | None = None,
    ) -> None:
        self.deepseek_client = deepseek_client
        self.deepseek_config = deepseek_config
        self.retriever = retriever
        self.retriever_config = retriever_config

    def run(
        self,
        analysis: SleepAnalysisResult,
        *,
        use_deepseek_report: bool = False,
    ) -> MockSleepReport:
        if use_deepseek_report:
            return generate_sleep_report_with_deepseek_fallback(
                analysis,
                client=self.deepseek_client,
                config=self.deepseek_config,
                retriever=self.retriever,
                retriever_config=self.retriever_config,
            )

        return generate_sleep_report(
            analysis,
            retriever=self.retriever,
            retriever_config=self.retriever_config,
        )
