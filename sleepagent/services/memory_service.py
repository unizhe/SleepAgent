from __future__ import annotations

from sleepagent.schemas import LongTermMemorySummary, SleepAgentTask
from sleepagent.services.data_management import SleepDataRepository
from sleepagent.services.memory import (
    DEFAULT_MEMORY_MAX_RECORDS,
    compress_memory_from_repository,
)
from sleepagent.services.memory_repository import MemoryRepository


class MemoryService:
    def __init__(
        self,
        *,
        data_repository: SleepDataRepository,
        memory_repository: MemoryRepository,
        max_records: int = DEFAULT_MEMORY_MAX_RECORDS,
    ) -> None:
        self.data_repository = data_repository
        self.memory_repository = memory_repository
        self.max_records = max_records

    def persist_task_memory(
        self,
        task: SleepAgentTask,
    ) -> LongTermMemorySummary | None:
        if task.analysis_result is None:
            return None

        analysis_record = self.data_repository.save_analysis(task.analysis_result)
        if task.report_result is not None:
            self.data_repository.save_report(
                task.report_result,
                analysis_id=analysis_record.analysis_id,
            )

        memory_summary = compress_memory_from_repository(
            self.data_repository,
            subject_id=task.analysis_result.metadata.patient.subject_id,
            max_records=self.max_records,
        )
        if memory_summary is None:
            return None
        return self.memory_repository.save_memory_summary(memory_summary)

