from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from sleepagent.schemas import LongTermMemorySummary


MEMORY_SUMMARIES_FILENAME = "memory_summaries.jsonl"


class MemoryRepository(Protocol):
    def save_memory_summary(
        self,
        memory_summary: LongTermMemorySummary,
    ) -> LongTermMemorySummary:
        ...

    def get_latest_memory_summary(
        self,
        subject_id: str,
    ) -> LongTermMemorySummary | None:
        ...


class LocalJsonlMemoryRepository:
    """Append-only memory repository used as local fallback and test adapter."""

    def __init__(self, store_dir: str | Path) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.store_dir / MEMORY_SUMMARIES_FILENAME

    def save_memory_summary(
        self,
        memory_summary: LongTermMemorySummary,
    ) -> LongTermMemorySummary:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(memory_summary.model_dump_json())
            file.write("\n")
        return memory_summary

    def get_latest_memory_summary(
        self,
        subject_id: str,
    ) -> LongTermMemorySummary | None:
        summaries = [
            LongTermMemorySummary.model_validate(payload)
            for payload in self._iter_json_lines()
            if payload.get("subject_id") == subject_id
        ]
        if not summaries:
            return None
        return max(summaries, key=lambda summary: summary.generated_at)

    def _iter_json_lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                stripped = line.strip()
                if stripped:
                    rows.append(json.loads(stripped))
        return rows

