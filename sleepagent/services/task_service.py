from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sleepagent.agents.report_agent import ReportAgent
from sleepagent.agents.task_graph import (
    build_default_task_plan,
    run_sleep_agent_task_state_graph,
)
from sleepagent.schemas.analysis import AnalysisRequest
from sleepagent.schemas.task import (
    AgentEvent,
    AgentEventType,
    Artifact,
    SleepAgentTask,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)
from sleepagent.services.analysis_service import AnalysisService
from sleepagent.services.artifact_repository import ArtifactRepository
from sleepagent.services.data_management import SleepDataRepository
from sleepagent.services.memory_repository import MemoryRepository
from sleepagent.services.memory_service import MemoryService
from sleepagent.services.repository_factory import build_repository_bundle
from sleepagent.services.task_repository import (
    TaskNotFoundError,
    TaskRepository,
)


class TaskServiceError(RuntimeError):
    """Base error for task lifecycle operations."""


class InvalidTaskTransitionError(TaskServiceError):
    """Raised when a task cannot move to the requested status."""


class TaskService:
    def __init__(
        self,
        *,
        repository: TaskRepository | None = None,
        artifact_repository: ArtifactRepository | None = None,
        memory_repository: MemoryRepository | None = None,
        data_repository: SleepDataRepository | None = None,
        analysis_service: AnalysisService | None = None,
        report_agent: ReportAgent | None = None,
        prefer_langgraph: bool = False,
    ) -> None:
        repository_bundle = None
        if (
            repository is None
            or artifact_repository is None
            or memory_repository is None
            or data_repository is None
        ):
            repository_bundle = build_repository_bundle()
        self.repository = repository or repository_bundle.task_repository  # type: ignore[union-attr]
        self.artifact_repository = (
            artifact_repository or repository_bundle.artifact_repository  # type: ignore[union-attr]
        )
        self.memory_repository = (
            memory_repository or repository_bundle.memory_repository  # type: ignore[union-attr]
        )
        self.data_repository = data_repository or repository_bundle.data_repository  # type: ignore[union-attr]
        self.analysis_service = analysis_service or AnalysisService()
        self.report_agent = report_agent or ReportAgent()
        self.prefer_langgraph = prefer_langgraph

    def create_task(self, request: TaskCreateRequest) -> SleepAgentTask:
        now = datetime.now(timezone.utc)
        analysis_request = _resolve_analysis_request(request)
        task = SleepAgentTask(
            id=f"task-{uuid4().hex[:12]}",
            title=request.title,
            user_goal=request.user_goal,
            status=TaskStatus.AWAITING_CONFIRMATION,
            patient_id=_resolve_patient_id(request, analysis_request),
            record_id=analysis_request.record_id,
            created_at=now,
            updated_at=now,
            analysis_request=analysis_request,
            use_deepseek_report=request.use_deepseek_report,
            user_question=request.user_question,
            plan=build_default_task_plan(),
        )
        events = [
            _event(
                task,
                1,
                AgentEventType.TASK_CREATED,
                "任务已创建",
                "SleepAgent 已创建任务并等待用户确认。",
                payload={
                    "record_id": task.record_id,
                    "patient_id": task.patient_id,
                    "status": task.status.value,
                },
            ),
            _event(
                task,
                2,
                AgentEventType.PLAN_CREATED,
                "执行计划已生成",
                "任务将按真实分析状态图顺序执行。",
                payload={
                    "plan": [
                        step.model_dump(mode="json", by_alias=True)
                        for step in task.plan
                    ]
                },
            ),
        ]
        task = task.model_copy(update={"events": events})
        self.repository.save_task(task)
        self.repository.append_events(task.id, events)
        return task

    def get_task(self, task_id: str) -> SleepAgentTask:
        return self._hydrate_task(self.repository.get_task(task_id))

    def list_events(self, task_id: str) -> list[AgentEvent]:
        self.repository.get_task(task_id)
        return self.repository.list_events(task_id)

    def list_artifacts(self, task_id: str) -> list[Artifact]:
        self.repository.get_task(task_id)
        artifacts = self.artifact_repository.list_artifacts(task_id)
        if artifacts:
            return artifacts
        return self.repository.get_task(task_id).artifacts

    def confirm_task(
        self,
        task_id: str,
        request: TaskConfirmRequest | None = None,
    ) -> SleepAgentTask:
        _ = request or TaskConfirmRequest()
        task = self.repository.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            return task
        if task.status not in {TaskStatus.AWAITING_CONFIRMATION, TaskStatus.FAILED}:
            raise InvalidTaskTransitionError(
                f"Task {task_id} cannot be confirmed from status {task.status.value}."
            )

        running_task = task.model_copy(
            update={
                "status": TaskStatus.RUNNING,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.repository.save_task(running_task)
        before_event_count = len(running_task.events)

        try:
            result = run_sleep_agent_task_state_graph(
                running_task,
                analysis_service=self.analysis_service,
                report_agent=self.report_agent,
                prefer_langgraph=self.prefer_langgraph,
            )
        except Exception as exc:
            result = self._fail_running_task(running_task, str(exc))

        new_events = result.events[before_event_count:]
        if new_events:
            self.repository.append_events(result.id, new_events)
        if result.artifacts:
            persisted_artifacts = self.artifact_repository.save_task_artifacts(
                result,
                result.artifacts,
            )
            result = result.model_copy(update={"artifacts": persisted_artifacts})
        if result.status == TaskStatus.COMPLETED:
            MemoryService(
                data_repository=self.data_repository,
                memory_repository=self.memory_repository,
            ).persist_task_memory(result)
        self.repository.save_task(result)
        return result

    def cancel_task(self, task_id: str) -> SleepAgentTask:
        task = self.repository.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            raise InvalidTaskTransitionError("Completed tasks cannot be cancelled.")
        if task.status == TaskStatus.FAILED:
            return task

        event = _event(
            task,
            len(task.events) + 1,
            AgentEventType.ERROR,
            "任务已取消",
            "用户在执行前取消了任务；状态按前端兼容约定标记为 failed。",
            payload={"error_code": "cancelled_by_user"},
        )
        updated = task.model_copy(
            update={
                "status": TaskStatus.FAILED,
                "events": [*task.events, event],
                "errors": [*task.errors, "cancelled_by_user"],
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.repository.append_events(updated.id, [event])
        self.repository.save_task(updated)
        return updated

    def _fail_running_task(self, task: SleepAgentTask, message: str) -> SleepAgentTask:
        event = _event(
            task,
            len(task.events) + 1,
            AgentEventType.ERROR,
            "任务执行失败",
            message,
            payload={"error_code": "task_graph_exception"},
        )
        return task.model_copy(
            update={
                "status": TaskStatus.FAILED,
                "events": [*task.events, event],
                "errors": [*task.errors, message],
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _hydrate_task(self, task: SleepAgentTask) -> SleepAgentTask:
        artifacts = self.artifact_repository.list_artifacts(task.id)
        if not artifacts:
            return task
        return task.model_copy(update={"artifacts": artifacts})


def _event(
    task: SleepAgentTask,
    sequence: int,
    event_type: AgentEventType,
    title: str,
    message: str,
    *,
    payload: dict | None = None,
) -> AgentEvent:
    return AgentEvent(
        id=f"{task.id}-event-{sequence:04d}",
        type=event_type,
        title=title,
        message=message,
        payload=payload,
    )


def _resolve_analysis_request(request: TaskCreateRequest) -> AnalysisRequest:
    if request.analysis_request is not None:
        return request.analysis_request
    return AnalysisRequest(
        record_id=request.record_id,
        subject_id=request.patient_id,
    )


def _resolve_patient_id(
    request: TaskCreateRequest,
    analysis_request: AnalysisRequest,
) -> str:
    if request.patient_id:
        return request.patient_id
    if analysis_request.subject_id:
        return analysis_request.subject_id
    if "-" in analysis_request.record_id:
        return analysis_request.record_id.split("-", maxsplit=1)[1]
    return analysis_request.record_id

