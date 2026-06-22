from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import (
    AnalysisMode,
    AnalysisRequest,
    AnalysisRunResult,
    SleepAgentOrchestrationRequest,
    SleepAnalysisResult,
)
from sleepagent.services.analysis_service import AnalysisService


class SleepAnalysisAgent:
    """Stage 8 boundary for producing one structured sleep analysis result."""

    def __init__(self, analysis_service: AnalysisService | None = None) -> None:
        self.analysis_service = analysis_service or AnalysisService()

    def run(self, request: SleepAgentOrchestrationRequest) -> SleepAnalysisResult:
        if request.analysis_mode == AnalysisMode.REAL:
            return self.analysis_service.run_sleep_analysis(
                _analysis_request_from_agent_request(request)
            )

        return generate_mock_sleep_analysis(
            record_id=request.record_id,
            subject_id=request.subject_id,
            duration_hours=request.duration_hours,
            seed=request.seed,
            abnormal_event_rate_per_hour=request.abnormal_event_rate_per_hour,
        )

    def run_full_analysis(
        self,
        request: SleepAgentOrchestrationRequest,
    ) -> AnalysisRunResult:
        if request.analysis_mode != AnalysisMode.REAL:
            raise ValueError("run_full_analysis is only available for real analysis mode.")
        return self.analysis_service.run_analysis(_analysis_request_from_agent_request(request))


def _analysis_request_from_agent_request(
    request: SleepAgentOrchestrationRequest,
) -> AnalysisRequest:
    return AnalysisRequest(
        record_id=request.record_id,
        subject_id=request.subject_id,
        shhs_root=request.shhs_root,
        edf_path=request.edf_path,
        nsrr_xml_path=request.nsrr_xml_path,
        profusion_xml_path=request.profusion_xml_path,
        eeg_channel=request.eeg_channel,
        eog_channel=request.eog_channel,
        emg_channel=request.emg_channel,
        respiratory_channels=list(request.respiratory_channels),
        use_respiratory_model=request.use_respiratory_model,
        respiratory_checkpoint_path=request.respiratory_checkpoint_path,
        allow_demo_respiratory_model=request.allow_demo_respiratory_model,
    )
