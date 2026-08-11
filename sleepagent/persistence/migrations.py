from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = "001_initial_schema"
_PERSISTENCE_ROOT = Path(__file__).parent
POSTGRES_BASELINE_SQL = (
    _PERSISTENCE_ROOT / "migrations" / f"{SCHEMA_VERSION}.sql"
).read_text(encoding="utf-8")
SQLITE_SCHEMA_SQL = (_PERSISTENCE_ROOT / "sqlite_schema.sql").read_text(
    encoding="utf-8"
)


def split_sql_statements(sql: str = POSTGRES_BASELINE_SQL) -> list[str]:
    statements: list[str] = []
    start = 0
    index = 0
    single_quoted = False
    double_quoted = False
    line_comment = False
    block_comment_depth = 0
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
        if line_comment:
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if sql.startswith("/*", index):
                block_comment_depth += 1
                index += 2
                continue
            if sql.startswith("*/", index):
                block_comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if single_quoted:
            if character == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            if character == "'":
                single_quoted = False
            index += 1
            continue
        if double_quoted:
            if character == '"' and index + 1 < len(sql) and sql[index + 1] == '"':
                index += 2
                continue
            if character == '"':
                double_quoted = False
            index += 1
            continue
        if sql.startswith("--", index):
            line_comment = True
            index += 2
            continue
        if sql.startswith("/*", index):
            block_comment_depth = 1
            index += 2
            continue
        if character == "'":
            single_quoted = True
            index += 1
            continue
        if character == '"':
            double_quoted = True
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


def apply_sqlite_schema(connection: sqlite3.Connection) -> None:
    if SCHEMA_VERSION in _sqlite_applied_versions(connection):
        return
    for statement in split_sql_statements(SQLITE_SCHEMA_SQL):
        sqlite_statement = _postgres_statement_to_sqlite(statement)
        if sqlite_statement:
            connection.execute(sqlite_statement)
    connection.commit()


def _sqlite_applied_versions(connection: sqlite3.Connection) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'sleepagent_schema_migrations'"
    ).fetchone()
    if exists is None:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT version FROM sleepagent_schema_migrations"
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
    "POSTGRES_BASELINE_SQL",
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_SQL",
    "apply_sqlite_schema",
    "split_sql_statements",
]
