from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from sleepagent.schemas import (
    AgentEvent,
    Artifact,
    ArtifactStatus,
    ArtifactVersion,
    LongTermMemorySummary,
    SleepAgentTask,
    StoredAnalysisRecord,
    StoredReportRecord,
)
from sleepagent.schemas.report import MockSleepReport
from sleepagent.schemas.sleep import SleepAnalysisResult
from sleepagent.services.artifact_repository import (
    ArtifactNotFoundError,
    ArtifactRepository,
    _build_artifact_version,
    _hydrate_artifact_metadata,
)
from sleepagent.services.data_management import (
    SleepDataRepository,
    build_stored_analysis_record,
    build_stored_report_record,
)
from sleepagent.services.memory_repository import MemoryRepository
from sleepagent.services.task_repository import TaskNotFoundError, TaskRepository


DATABASE_URL_ENV = "DATABASE_URL"
POSTGRES_CONNECT_TIMEOUT_SECONDS = 3

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subjects (
  subject_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sleep_records (
  record_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  source_dataset TEXT NOT NULL,
  record_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  record_id TEXT NOT NULL,
  status TEXT NOT NULL,
  task_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  step_id TEXT,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_results (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  record_id TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  stored_at TIMESTAMPTZ NOT NULL,
  analysis_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  analysis_id TEXT,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  record_id TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  stored_at TIMESTAMPTZ NOT NULL,
  report_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  record_id TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  current_version_id TEXT,
  created_by_step_id TEXT NOT NULL,
  artifact_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_versions (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  content TEXT NOT NULL,
  revision_instruction TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  safety_review_status TEXT NOT NULL,
  blocked_reasons JSONB NOT NULL,
  reviewed_at TIMESTAMPTZ,
  reviewed_by TEXT,
  version_json JSONB NOT NULL,
  UNIQUE(artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS memory_summaries (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
  generated_at TIMESTAMPTZ NOT NULL,
  summary_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
  id TEXT PRIMARY KEY,
  subject_id TEXT REFERENCES subjects(subject_id) ON DELETE CASCADE,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS external_contexts (
  id TEXT PRIMARY KEY,
  subject_id TEXT REFERENCES subjects(subject_id) ON DELETE CASCADE,
  context_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class PostgresUnavailableError(RuntimeError):
    """Raised when PostgreSQL repository setup is requested but unavailable."""


def split_postgres_schema_statements(schema_sql: str = POSTGRES_SCHEMA_SQL) -> list[str]:
    return [
        statement.strip()
        for statement in schema_sql.split(";")
        if statement.strip()
    ]


def initialize_postgres_schema(database_url: str | None = None) -> None:
    resolved_url = database_url or os.getenv(DATABASE_URL_ENV)
    if not resolved_url:
        raise PostgresUnavailableError(f"{DATABASE_URL_ENV} is not configured.")
    psycopg = _load_psycopg()
    with _connect(psycopg, resolved_url) as connection:
        with connection.cursor() as cursor:
            for statement in split_postgres_schema_statements():
                cursor.execute(statement)
        connection.commit()


class PostgresTaskRepository(TaskRepository):
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = _require_database_url(database_url)
        initialize_postgres_schema(self.database_url)

    def save_task(self, task: SleepAgentTask) -> SleepAgentTask:
        _ensure_subject(self.database_url, task.patient_id)
        payload = task.model_dump(mode="json")
        _execute(
            self.database_url,
            """
            INSERT INTO tasks (id, subject_id, record_id, status, task_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              subject_id = EXCLUDED.subject_id,
              record_id = EXCLUDED.record_id,
              status = EXCLUDED.status,
              task_json = EXCLUDED.task_json,
              updated_at = EXCLUDED.updated_at
            """,
            (
                task.id,
                task.patient_id,
                task.record_id,
                task.status.value,
                json.dumps(payload, ensure_ascii=False),
                task.created_at,
                task.updated_at,
            ),
        )
        return task

    def get_task(self, task_id: str) -> SleepAgentTask:
        row = _fetchone(
            self.database_url,
            "SELECT task_json FROM tasks WHERE id = %s",
            (task_id,),
        )
        if row is None:
            raise TaskNotFoundError(task_id)
        task = SleepAgentTask.model_validate(_json_column(row[0]))
        events = self.list_events(task_id)
        return task.model_copy(update={"events": events})

    def list_events(self, task_id: str) -> list[AgentEvent]:
        rows = _fetchall(
            self.database_url,
            "SELECT event_json FROM task_events WHERE task_id = %s ORDER BY created_at, id",
            (task_id,),
        )
        return [AgentEvent.model_validate(_json_column(row[0])) for row in rows]

    def append_events(self, task_id: str, events: list[AgentEvent]) -> list[AgentEvent]:
        for event in events:
            _execute(
                self.database_url,
                """
                INSERT INTO task_events (id, task_id, event_type, step_id, event_json, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    event.id,
                    task_id,
                    event.type.value,
                    event.step_id,
                    json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                    event.timestamp,
                ),
            )
        return self.list_events(task_id)


class PostgresArtifactRepository(ArtifactRepository):
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = _require_database_url(database_url)
        initialize_postgres_schema(self.database_url)

    def save_task_artifacts(
        self,
        task: SleepAgentTask,
        artifacts: list[Artifact],
    ) -> list[Artifact]:
        _ensure_subject(self.database_url, task.patient_id)
        saved: list[Artifact] = []
        for artifact in artifacts:
            existing = _fetchone(
                self.database_url,
                "SELECT artifact_json FROM artifacts WHERE id = %s",
                (artifact.id,),
            )
            if existing is not None:
                saved.append(Artifact.model_validate(_json_column(existing[0])))
                continue
            version = _build_artifact_version(
                artifact.id,
                content=artifact.content,
                artifact_type=artifact.type,
                version_number=1,
                revision_instruction="Initial artifact generated by task graph.",
                created_by=artifact.created_by_step_id,
            )
            hydrated = _hydrate_artifact_metadata(
                artifact,
                task=task,
                current_version_id=version.id,
                status=artifact.status,
            )
            self._upsert_artifact(hydrated)
            self._insert_version(version)
            saved.append(hydrated)
        return saved

    def get_artifact(self, artifact_id: str) -> Artifact:
        row = _fetchone(
            self.database_url,
            "SELECT artifact_json FROM artifacts WHERE id = %s",
            (artifact_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(artifact_id)
        return Artifact.model_validate(_json_column(row[0]))

    def list_artifacts(self, task_id: str) -> list[Artifact]:
        rows = _fetchall(
            self.database_url,
            "SELECT artifact_json FROM artifacts WHERE task_id = %s ORDER BY created_at, id",
            (task_id,),
        )
        return [Artifact.model_validate(_json_column(row[0])) for row in rows]

    def revise_artifact(
        self,
        artifact_id: str,
        *,
        content: str,
        revision_instruction: str,
        created_by: str,
    ) -> tuple[Artifact, ArtifactVersion]:
        artifact = self.get_artifact(artifact_id)
        version = _build_artifact_version(
            artifact_id,
            content=content,
            artifact_type=artifact.type,
            version_number=len(self.list_versions(artifact_id)) + 1,
            revision_instruction=revision_instruction,
            created_by=created_by,
        )
        updated = artifact.model_copy(
            update={
                "status": ArtifactStatus.REVISED,
                "content": content,
                "current_version_id": version.id,
                "updated_at": version.created_at,
            }
        )
        self._upsert_artifact(updated)
        self._insert_version(version)
        return updated, version

    def confirm_artifact(
        self,
        artifact_id: str,
        *,
        confirmed_by: str,
    ) -> Artifact:
        _ = confirmed_by
        artifact = self.get_artifact(artifact_id)
        updated = artifact.model_copy(
            update={
                "status": ArtifactStatus.READY,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._upsert_artifact(updated)
        return updated

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        rows = _fetchall(
            self.database_url,
            "SELECT version_json FROM artifact_versions WHERE artifact_id = %s ORDER BY version_number",
            (artifact_id,),
        )
        return [ArtifactVersion.model_validate(_json_column(row[0])) for row in rows]

    def _upsert_artifact(self, artifact: Artifact) -> None:
        if artifact.task_id is None or artifact.subject_id is None or artifact.record_id is None:
            raise ValueError("artifact must include task_id, subject_id and record_id.")
        payload = json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False)
        _execute(
            self.database_url,
            """
            INSERT INTO artifacts (
              id, task_id, subject_id, record_id, type, title, status,
              current_version_id, created_by_step_id, artifact_json, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              status = EXCLUDED.status,
              current_version_id = EXCLUDED.current_version_id,
              artifact_json = EXCLUDED.artifact_json,
              updated_at = EXCLUDED.updated_at
            """,
            (
                artifact.id,
                artifact.task_id,
                artifact.subject_id,
                artifact.record_id,
                artifact.type.value,
                artifact.title,
                artifact.status.value,
                artifact.current_version_id,
                artifact.created_by_step_id,
                payload,
                artifact.created_at,
                artifact.updated_at,
            ),
        )

    def _insert_version(self, version: ArtifactVersion) -> None:
        _execute(
            self.database_url,
            """
            INSERT INTO artifact_versions (
              id, artifact_id, version_number, content, revision_instruction,
              created_by, created_at, safety_review_status, blocked_reasons,
              reviewed_at, reviewed_by, version_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                version.id,
                version.artifact_id,
                version.version_number,
                version.content,
                version.revision_instruction,
                version.created_by,
                version.created_at,
                version.safety_review_status.value,
                json.dumps(version.blocked_reasons, ensure_ascii=False),
                version.reviewed_at,
                version.reviewed_by,
                json.dumps(version.model_dump(mode="json"), ensure_ascii=False),
            ),
        )


class PostgresMemoryRepository(MemoryRepository):
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = _require_database_url(database_url)
        initialize_postgres_schema(self.database_url)

    def save_memory_summary(
        self,
        memory_summary: LongTermMemorySummary,
    ) -> LongTermMemorySummary:
        _ensure_subject(self.database_url, memory_summary.subject_id)
        memory_id = _memory_summary_id(memory_summary)
        _execute(
            self.database_url,
            """
            INSERT INTO memory_summaries (id, subject_id, generated_at, summary_json)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET summary_json = EXCLUDED.summary_json
            """,
            (
                memory_id,
                memory_summary.subject_id,
                memory_summary.generated_at,
                json.dumps(memory_summary.model_dump(mode="json"), ensure_ascii=False),
            ),
        )
        return memory_summary

    def get_latest_memory_summary(
        self,
        subject_id: str,
    ) -> LongTermMemorySummary | None:
        row = _fetchone(
            self.database_url,
            """
            SELECT summary_json FROM memory_summaries
            WHERE subject_id = %s
            ORDER BY generated_at DESC, id DESC
            LIMIT 1
            """,
            (subject_id,),
        )
        if row is None:
            return None
        return LongTermMemorySummary.model_validate(_json_column(row[0]))


class PostgresSleepDataRepository(SleepDataRepository):
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = _require_database_url(database_url)
        initialize_postgres_schema(self.database_url)

    def save_analysis(
        self,
        analysis: SleepAnalysisResult,
        *,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredAnalysisRecord:
        record = build_stored_analysis_record(
            analysis,
            analysis_id=analysis_id,
            stored_at=stored_at,
        )
        _ensure_subject(self.database_url, record.subject_id)
        _upsert_sleep_record_for_analysis(self.database_url, analysis)
        _execute(
            self.database_url,
            """
            INSERT INTO analysis_results (
              id, subject_id, record_id, risk_level, generated_at, stored_at, analysis_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET analysis_json = EXCLUDED.analysis_json
            """,
            (
                record.analysis_id,
                record.subject_id,
                record.record_id,
                record.risk_level.value,
                record.generated_at,
                record.stored_at,
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
            ),
        )
        return record

    def save_report(
        self,
        report: MockSleepReport,
        *,
        report_id: str | None = None,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredReportRecord:
        record = build_stored_report_record(
            report,
            report_id=report_id,
            analysis_id=analysis_id,
            stored_at=stored_at,
        )
        _ensure_subject(self.database_url, record.subject_id)
        _execute(
            self.database_url,
            """
            INSERT INTO reports (
              id, analysis_id, subject_id, record_id, risk_level, generated_at, stored_at, report_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET report_json = EXCLUDED.report_json
            """,
            (
                record.report_id,
                record.analysis_id,
                record.subject_id,
                record.record_id,
                record.risk_level.value,
                record.generated_at,
                record.stored_at,
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
            ),
        )
        return record

    def list_analysis_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredAnalysisRecord]:
        rows = _fetchall(
            self.database_url,
            """
            SELECT analysis_json FROM analysis_results
            WHERE (%s IS NULL OR subject_id = %s)
              AND (%s IS NULL OR record_id = %s)
            ORDER BY generated_at DESC, stored_at DESC
            """,
            (subject_id, subject_id, record_id, record_id),
        )
        return [StoredAnalysisRecord.model_validate(_json_column(row[0])) for row in rows]

    def list_report_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredReportRecord]:
        rows = _fetchall(
            self.database_url,
            """
            SELECT report_json FROM reports
            WHERE (%s IS NULL OR subject_id = %s)
              AND (%s IS NULL OR record_id = %s)
            ORDER BY generated_at DESC, stored_at DESC
            """,
            (subject_id, subject_id, record_id, record_id),
        )
        return [StoredReportRecord.model_validate(_json_column(row[0])) for row in rows]

    def get_latest_analysis_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredAnalysisRecord | None:
        records = self.list_analysis_records(subject_id=subject_id, record_id=record_id)
        return records[0] if records else None

    def get_latest_report_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredReportRecord | None:
        records = self.list_report_records(subject_id=subject_id, record_id=record_id)
        return records[0] if records else None


def _require_database_url(database_url: str | None = None) -> str:
    resolved = database_url or os.getenv(DATABASE_URL_ENV)
    if not resolved:
        raise PostgresUnavailableError(f"{DATABASE_URL_ENV} is not configured.")
    return resolved


def _load_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise PostgresUnavailableError(
            "psycopg is required for PostgreSQL repositories. "
            "Install project dependencies with the PostgreSQL extra."
        ) from exc
    return psycopg


def _ensure_subject(database_url: str, subject_id: str) -> None:
    _execute(
        database_url,
        "INSERT INTO subjects (subject_id) VALUES (%s) ON CONFLICT (subject_id) DO NOTHING",
        (subject_id,),
    )


def _upsert_sleep_record_for_analysis(
    database_url: str,
    analysis: SleepAnalysisResult,
) -> None:
    _execute(
        database_url,
        """
        INSERT INTO sleep_records (record_id, subject_id, source_dataset, record_json)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (record_id) DO UPDATE SET
          subject_id = EXCLUDED.subject_id,
          source_dataset = EXCLUDED.source_dataset,
          record_json = EXCLUDED.record_json
        """,
        (
            analysis.metadata.record_id,
            analysis.metadata.patient.subject_id,
            analysis.metadata.source_dataset,
            json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False),
        ),
    )


def _execute(database_url: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    psycopg = _load_psycopg()
    with _connect(psycopg, database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
        connection.commit()


def _fetchone(
    database_url: str,
    sql: str,
    params: tuple[Any, ...] = (),
) -> tuple[Any, ...] | None:
    psycopg = _load_psycopg()
    with _connect(psycopg, database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()


def _fetchall(
    database_url: str,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[tuple[Any, ...]]:
    psycopg = _load_psycopg()
    with _connect(psycopg, database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())


def _connect(psycopg: Any, database_url: str) -> Any:
    return psycopg.connect(
        database_url,
        connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
    )


def _json_column(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _memory_summary_id(memory_summary: LongTermMemorySummary) -> str:
    timestamp = memory_summary.generated_at.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"memory-{memory_summary.subject_id}-{timestamp}"
