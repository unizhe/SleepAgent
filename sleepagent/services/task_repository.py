from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from sleepagent.schemas import AgentEvent, SleepAgentTask


TASK_RECORDS_FILENAME = "tasks.jsonl"
TASK_EVENTS_FILENAME = "task_events.jsonl"


class TaskNotFoundError(KeyError):
    """Raised when a task id is not present in the task repository."""


class TaskRepository(Protocol):
    def save_task(self, task: SleepAgentTask) -> SleepAgentTask:
        ...

    def get_task(self, task_id: str) -> SleepAgentTask:
        ...

    def list_events(self, task_id: str) -> list[AgentEvent]:
        ...

    def append_events(self, task_id: str, events: list[AgentEvent]) -> list[AgentEvent]:
        ...


class LocalJsonlTaskRepository:
    """Append-only local task store used until the Phase 4 PostgreSQL repository."""

    def __init__(self, store_dir: str | Path) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_path = self.store_dir / TASK_RECORDS_FILENAME
        self.events_path = self.store_dir / TASK_EVENTS_FILENAME

    def save_task(self, task: SleepAgentTask) -> SleepAgentTask:
        self._append_json_line(self.tasks_path, task.model_dump(mode="json"))
        return task

    def get_task(self, task_id: str) -> SleepAgentTask:
        latest: SleepAgentTask | None = None
        for payload in self._iter_json_lines(self.tasks_path):
            if payload.get("id") == task_id:
                latest = SleepAgentTask.model_validate(payload)
        if latest is None:
            raise TaskNotFoundError(task_id)

        events = self.list_events(task_id)
        if events:
            latest = latest.model_copy(update={"events": events})
        return latest

    def list_events(self, task_id: str) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for payload in self._iter_json_lines(self.events_path):
            if payload.get("task_id") == task_id:
                events.append(AgentEvent.model_validate(payload["event"]))
        return events

    def append_events(self, task_id: str, events: list[AgentEvent]) -> list[AgentEvent]:
        for event in events:
            self._append_json_line(
                self.events_path,
                {
                    "task_id": task_id,
                    "event": event.model_dump(mode="json"),
                },
            )
        return self.list_events(task_id)

    def _append_json_line(self, path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _iter_json_lines(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                stripped = line.strip()
                if stripped:
                    rows.append(json.loads(stripped))
        return rows

