from __future__ import annotations

import sqlite3
from pathlib import Path


MIGRATION_VERSIONS = (
    "001_radar_agent_persistence",
    "002_dynamic_agent_runtime",
    "003_habit_profile",
    "004_habit_question_suppression",
    "005_habit_questionnaire_state",
    "006_product_agent_state",
    "007_longitudinal_memory_governance",
    "008_longitudinal_authority_cas",
    "009_human_decision_governance",
    "010_sleep_domain_foundation",
    "011_adapter_registry_control",
    "012_device_binding_promotion",
    "013_perceptor_push_ingestion",
    "014_perceptor_pull_reconciliation",
    "015_night_episode_lifecycle",
    "016_deterministic_fast_path",
    "017_product_agent_bridge",
    "018_sleep_api_v1",
    "019_sleep_api_event_polling",
    "020_legacy_authority_cutover",
)
MIGRATION_VERSION = MIGRATION_VERSIONS[-1]
_MIGRATION_DIR = Path(__file__).parent / "migrations"
RADAR_AGENT_POSTGRES_MIGRATIONS = {
    version: (_MIGRATION_DIR / f"{version}.sql").read_text(encoding="utf-8")
    for version in MIGRATION_VERSIONS
}
RADAR_AGENT_POSTGRES_MIGRATION_SQL = "\n".join(
    RADAR_AGENT_POSTGRES_MIGRATIONS[version] for version in MIGRATION_VERSIONS
)


def split_sql_statements(sql: str = RADAR_AGENT_POSTGRES_MIGRATION_SQL) -> list[str]:
    statements: list[str] = []
    start = 0
    index = 0
    single_quoted = False
    dollar_tag: str | None = None
    while index < len(sql):
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
                continue
            index += 1
            continue
        character = sql[index]
        if single_quoted:
            if character == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            if character == "'":
                single_quoted = False
            index += 1
            continue
        if character == "'":
            single_quoted = True
            index += 1
            continue
        if character == "$":
            end = sql.find("$", index + 1)
            if end != -1:
                candidate = sql[index : end + 1]
                if candidate == "$$" or candidate[1:-1].replace("_", "").isalnum():
                    dollar_tag = candidate
                    index = end + 1
                    continue
        if character == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def apply_sqlite_migration(connection: sqlite3.Connection) -> None:
    applied = _sqlite_applied_versions(connection)
    for version in MIGRATION_VERSIONS:
        if version in applied:
            continue
        for statement in split_sql_statements(
            RADAR_AGENT_POSTGRES_MIGRATIONS[version]
        ):
            sqlite_statement = _postgres_statement_to_sqlite(statement)
            if sqlite_statement:
                connection.execute(sqlite_statement)
    connection.commit()


def _sqlite_applied_versions(connection: sqlite3.Connection) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'radar_agent_schema_migrations'"
    ).fetchone()
    if exists is None:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT version FROM radar_agent_schema_migrations"
        ).fetchall()
    }


def _postgres_statement_to_sqlite(statement: str) -> str:
    normalized = " ".join(statement.split())
    if normalized.startswith(
        "CREATE OR REPLACE FUNCTION sleep_domain_reject_raw_mutation"
    ):
        return ""
    if normalized.startswith(
        "CREATE TRIGGER sleep_domain_raw_inbox_immutable_update"
    ):
        return (
            "CREATE TRIGGER IF NOT EXISTS "
            "sleep_domain_raw_inbox_immutable_update "
            "BEFORE UPDATE ON sleep_domain_raw_inbox "
            "BEGIN SELECT RAISE(ABORT, 'sleep_domain_raw_inbox is immutable'); END"
        )
    if normalized.startswith(
        "CREATE TRIGGER sleep_domain_raw_inbox_immutable_delete"
    ):
        return (
            "CREATE TRIGGER IF NOT EXISTS "
            "sleep_domain_raw_inbox_immutable_delete "
            "BEFORE DELETE ON sleep_domain_raw_inbox "
            "BEGIN SELECT RAISE(ABORT, 'sleep_domain_raw_inbox is immutable'); END"
        )
    converted = statement
    replacements = {
        "BIGSERIAL": "INTEGER",
        "JSONB": "TEXT",
        "TIMESTAMPTZ": "TEXT",
        "DOUBLE PRECISION": "REAL",
        "BOOLEAN": "INTEGER",
        "DATE": "TEXT",
        " DEFAULT NOW()": "",
    }
    for old, new in replacements.items():
        converted = converted.replace(old, new)
    return converted


__all__ = [
    "MIGRATION_VERSION",
    "MIGRATION_VERSIONS",
    "RADAR_AGENT_POSTGRES_MIGRATION_SQL",
    "RADAR_AGENT_POSTGRES_MIGRATIONS",
    "apply_sqlite_migration",
    "split_sql_statements",
]
