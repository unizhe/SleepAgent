from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas.analysis import AnalysisMode
from sleepagent.schemas.report import MockSleepReport
from sleepagent.schemas.sleep import SleepAnalysisResult

NonEmptyStr = Annotated[str, Field(min_length=1)]


class StrictAgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentStepName(str, Enum):
    SLEEP_ANALYSIS = "sleep_analysis"
    REPORT = "report"
    DIALOGUE = "dialogue"
    SKIP_DIALOGUE = "skip_dialogue"


class AgentStepStatus(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"


class AgentOrchestrationMode(str, Enum):
    LINEAR = "linear"
    LANGGRAPH = "langgraph"


class SleepAgentOrchestrationRequest(StrictAgentSchema):
    record_id: str = Field(default="mock-shhs-0001", min_length=1)
    subject_id: str = Field(default="mock-subject-0001", min_length=1)
    duration_hours: float = Field(default=8.0, ge=0.5, le=12.0)
    seed: int = 42
    abnormal_event_rate_per_hour: float = Field(default=6.0, ge=0.0, le=60.0)
    user_question: str | None = Field(default=None, min_length=1)
    dialogue_context: "DialogueContext | None" = None
    use_deepseek_report: bool = False
    analysis_mode: AnalysisMode = AnalysisMode.MOCK
    shhs_root: str | None = Field(default=None, min_length=1)
    edf_path: str | None = Field(default=None, min_length=1)
    nsrr_xml_path: str | None = Field(default=None, min_length=1)
    profusion_xml_path: str | None = Field(default=None, min_length=1)
    eeg_channel: str = Field(default="EEG", min_length=1)
    eog_channel: str | None = Field(default="EOG(L)", min_length=1)
    emg_channel: str | None = Field(default="EMG", min_length=1)
    respiratory_channels: list[NonEmptyStr] = Field(default_factory=list)
    use_respiratory_model: bool = False
    respiratory_checkpoint_path: str | None = Field(default=None, min_length=1)
    allow_demo_respiratory_model: bool = False


class SleepAgentEndpointRequest(SleepAgentOrchestrationRequest):
    use_langgraph: bool = False


class AgentStepTrace(StrictAgentSchema):
    step_name: AgentStepName
    status: AgentStepStatus
    message: str = Field(..., min_length=1)


class DialogueContext(StrictAgentSchema):
    history_summary: str | None = Field(default=None, min_length=1)
    user_preferences: list[NonEmptyStr] = Field(default_factory=list)
    recent_questions: list[NonEmptyStr] = Field(default_factory=list)


class DialogueTurn(StrictAgentSchema):
    user_question: str = Field(..., min_length=1)
    assistant_response: str = Field(..., min_length=1)
    safety_flags: list[str] = Field(default_factory=list)
    referenced_record_id: str = Field(..., min_length=1)
    context_used: bool = False


class SleepAgentOrchestrationResult(StrictAgentSchema):
    analysis: SleepAnalysisResult
    report: MockSleepReport
    dialogue: DialogueTurn | None = None
    steps: list[AgentStepTrace] = Field(default_factory=list)
    orchestration_mode: AgentOrchestrationMode = AgentOrchestrationMode.LINEAR
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
