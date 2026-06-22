from dataclasses import dataclass, field

from sleepagent.agents.dialogue_agent import DialogueAgent
from sleepagent.agents.report_agent import ReportAgent
from sleepagent.agents.sleep_analysis_agent import SleepAnalysisAgent
from sleepagent.schemas import (
    AgentOrchestrationMode,
    AgentStepName,
    AgentStepStatus,
    AgentStepTrace,
    SleepAgentOrchestrationRequest,
    SleepAgentOrchestrationResult,
)
from sleepagent.services.repository_factory import build_repository_bundle


@dataclass
class SleepAgentOrchestrator:
    sleep_analysis_agent: SleepAnalysisAgent = field(default_factory=SleepAnalysisAgent)
    report_agent: ReportAgent = field(default_factory=ReportAgent)
    dialogue_agent: DialogueAgent = field(
        default_factory=lambda: DialogueAgent(
            memory_repository=build_repository_bundle().memory_repository
        )
    )

    def run(
        self,
        request: SleepAgentOrchestrationRequest,
    ) -> SleepAgentOrchestrationResult:
        steps: list[AgentStepTrace] = []

        analysis = self.sleep_analysis_agent.run(request)
        steps.append(
            AgentStepTrace(
                step_name=AgentStepName.SLEEP_ANALYSIS,
                status=AgentStepStatus.COMPLETED,
                message="Generated structured sleep analysis result.",
            )
        )

        report = self.report_agent.run(
            analysis,
            use_deepseek_report=request.use_deepseek_report,
        )
        steps.append(
            AgentStepTrace(
                step_name=AgentStepName.REPORT,
                status=AgentStepStatus.COMPLETED,
                message="Generated report from the analysis result.",
            )
        )

        dialogue = None
        if request.user_question:
            dialogue = self.dialogue_agent.run(
                user_question=request.user_question,
                analysis=analysis,
                report=report,
                dialogue_context=request.dialogue_context,
            )
            steps.append(
                AgentStepTrace(
                    step_name=AgentStepName.DIALOGUE,
                    status=AgentStepStatus.COMPLETED,
                    message="Answered the user question using the report context.",
                )
            )
        else:
            steps.append(
                AgentStepTrace(
                    step_name=AgentStepName.SKIP_DIALOGUE,
                    status=AgentStepStatus.SKIPPED,
                    message="No user question was provided.",
                )
            )

        return SleepAgentOrchestrationResult(
            analysis=analysis,
            report=report,
            dialogue=dialogue,
            steps=steps,
            orchestration_mode=AgentOrchestrationMode.LINEAR,
            generated_at=analysis.generated_at,
        )


def run_sleep_agent_orchestration(
    request: SleepAgentOrchestrationRequest | None = None,
    **request_overrides: object,
) -> SleepAgentOrchestrationResult:
    if request is not None and request_overrides:
        raise ValueError("Pass either request or request_overrides, not both.")

    resolved_request = request or SleepAgentOrchestrationRequest.model_validate(
        request_overrides
    )
    return SleepAgentOrchestrator().run(resolved_request)
