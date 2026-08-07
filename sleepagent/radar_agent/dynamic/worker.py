from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from time import sleep
from uuid import uuid4

from sleepagent.radar_agent.dynamic.contracts import (
    AgentInvocationStatus,
    InvocationOutcome,
)
from sleepagent.radar_agent.runtime import RadarAgentTask, RadarTaskStatus, TaskService

from .orchestrator import DynamicOrchestratorRuntime


RunnerFactory = Callable[[RadarAgentTask], DynamicOrchestratorRuntime]


class DynamicTaskWorker:
    """Database-leased worker; HTTP requests only wake it, never own execution."""

    def __init__(
        self,
        *,
        service: TaskService,
        runner_factory: RunnerFactory,
        worker_id: str | None = None,
        poll_interval_seconds: float = 0.2,
        lease_seconds: int = 120,
    ) -> None:
        self.service = service
        self.store = service.store
        self.runner_factory = runner_factory
        self.worker_id = worker_id or f"worker-{uuid4().hex}"
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.recover_interrupted_attempts()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"sleepagent-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        tasks = self.store.list_tasks(
            statuses={
                RadarTaskStatus.RUNNING,
                RadarTaskStatus.WAITING_FOR_USER_INPUT,
            }
        )
        for task in tasks:
            if task.runtime_kind != "dynamic_goal":
                continue
            if task.status == RadarTaskStatus.WAITING_FOR_USER_INPUT:
                request_id = task.pending_user_input_request_id
                if not request_id:
                    continue
                request = self.store.get_user_input_request(request_id)
                if (
                    request.status != "declined"
                    and self.store.get_user_input_response(request_id) is None
                ):
                    continue
            lease = self.store.acquire_job_lease(
                task.task_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if lease is None:
                continue
            try:
                self.runner_factory(task).run(task.task_id)
            except Exception as exc:
                current = self.service.get_task(task.task_id)
                if current.status == RadarTaskStatus.RUNNING:
                    self.service.fail_task(
                        task.task_id,
                        error_code="dynamic_worker_failed",
                        message="Dynamic execution stopped safely.",
                        retryable=True,
                        details={"error_code": exc.__class__.__name__},
                    )
            finally:
                self.store.release_job_lease(
                    task.task_id, lease_token=lease.lease_token
                )
            return True
        return False

    def recover_interrupted_attempts(self) -> int:
        recovered = 0
        for task in self.store.list_tasks(statuses={RadarTaskStatus.RUNNING}):
            if task.runtime_kind != "dynamic_goal":
                continue
            task_recovered = 0
            for value in self.store.list_model_invocations(task.task_id):
                if value.outcome == InvocationOutcome.PENDING:
                    self.store.save_model_invocation(
                        value.model_copy(
                            update={
                                "outcome": InvocationOutcome.UNKNOWN_OUTCOME,
                                "error_code": "worker_recovery_unknown_outcome",
                                "finished_at": datetime.now(timezone.utc),
                            }
                        )
                    )
                    recovered += 1
                    task_recovered += 1
            for value in self.store.list_tool_invocations(task.task_id):
                if value.outcome == InvocationOutcome.PENDING:
                    self.store.save_tool_invocation(
                        value.model_copy(
                            update={
                                "outcome": InvocationOutcome.UNKNOWN_OUTCOME,
                                "error_code": "worker_recovery_unknown_outcome",
                                "finished_at": datetime.now(timezone.utc),
                            }
                        )
                    )
                    recovered += 1
                    task_recovered += 1
            for value in self.store.list_agent_invocations(task.task_id):
                if value.status in {
                    AgentInvocationStatus.PENDING,
                    AgentInvocationStatus.RUNNING,
                }:
                    self.store.save_agent_invocation(
                        value.model_copy(
                            update={
                                "status": AgentInvocationStatus.UNKNOWN_OUTCOME,
                                "error_summary": "worker_recovery_unknown_outcome",
                                "finished_at": datetime.now(timezone.utc),
                            }
                        )
                    )
                    recovered += 1
                    task_recovered += 1
            if task_recovered:
                self.service.emit_event(
                    task.task_id,
                    event_type="task.recovered",
                    message="Interrupted dynamic attempts were marked unknown before resume.",
                    payload={"unknown_outcome_count": task_recovered},
                )
        return recovered

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = self.run_once()
            if worked:
                continue
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()


__all__ = ["DynamicTaskWorker", "RunnerFactory"]
