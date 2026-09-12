# 本模块固定迁移清单、校验和及不可变数据库标识。
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "001_initial_schema"
# 这些名称已经写入 003--005 迁移和持久化审计记录；运行时代码只引用语义常量，
# 避免把早期施工阶段命名继续扩散到业务模块。
COMMAND_AUTHORITY_FUNCTION = "sleepagent_stage2_authority_allows"
SCENARIO_CLOCK_AUTHORITY_FUNCTION = "sleepagent_stage3_advance_authority_allows"
DELIVERY_AUTHORITY_FUNCTION = "sleepagent_stage4_delivery_authority_allows"
COMMAND_AUTHORITY_FUNCTION_SIGNATURE = (
    f"public.{COMMAND_AUTHORITY_FUNCTION}(text,text,text)"
)
SCENARIO_CLOCK_AUTHORITY_FUNCTION_SIGNATURE = (
    f"public.{SCENARIO_CLOCK_AUTHORITY_FUNCTION}(text)"
)
DELIVERY_AUTHORITY_FUNCTION_SIGNATURE = (
    f"public.{DELIVERY_AUTHORITY_FUNCTION}(text)"
)
COMMAND_REGISTRY_VERSION = "stage2-command-registry.v1"
DETERMINISTIC_INTERACTION_MODEL_VERSION = "deterministic-stage2-interaction.v1"
COMMAND_COMMITTED_EVENT_TYPE = "STAGE2_COMMAND_COMMITTED"
COMMAND_COMMITTED_AUDIT_REASON = "stage2_command_committed"
MODEL_REQUEST_SCHEMA_VERSION = "stage2_model_request.v1"
COMMAND_INVOCATION_NAMESPACE = "stage2"
COMMAND_AUTHORIZATION_REVOKED_ERROR = "stage2_authorization_revoked"
COMMAND_STATE_CONFLICT_ERROR = "stage2_state_conflict"
COMMAND_INVARIANT_ERROR = "stage2_invariant_violation"
COMMAND_LEASE_LOST_ERROR = "stage2_lease_lost_reconciliation_required"
_PERSISTENCE_ROOT = Path(__file__).parent
MIGRATION_MANIFEST_PATH = _PERSISTENCE_ROOT / "migration_manifest.json"
POSTGRES_BASELINE_SQL = (
    _PERSISTENCE_ROOT / "migrations" / f"{SCHEMA_VERSION}.sql"
).read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class MigrationManifestEntry:
    version: int
    filename: str
    sha256: str
    transactional: bool
    minimum_app_release: str


@dataclass(frozen=True, slots=True)
class MigrationReleaseManifest:
    schema_version: str
    target_version: int
    migrations: tuple[MigrationManifestEntry, ...]
    manifest_sha256: str


def migration_identity(entry: MigrationManifestEntry) -> str:
    """Return the stable ledger identity attested by every runtime process."""

    strategy = "transactional" if entry.transactional else "nontransactional"
    name = entry.filename.removesuffix(".sql")
    return f"{entry.version:03d}:{name}:{entry.sha256}:{strategy}"


def load_migration_manifest(
    path: Path = MIGRATION_MANIFEST_PATH,
) -> MigrationReleaseManifest:
    """Load the exact ordered migration release declared by the package."""

    raw = path.read_bytes()
    try:
        payload: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("migration manifest is not canonical UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("migration manifest must be a JSON object")
    if payload.get("schema_version") != "sleepagent_migration_manifest.v1":
        raise ValueError("unsupported migration manifest schema")
    target_version = payload.get("target_version")
    records = payload.get("migrations")
    if not isinstance(target_version, int) or target_version < 1:
        raise ValueError("migration manifest target_version is invalid")
    if not isinstance(records, list) or not records:
        raise ValueError("migration manifest migrations must be non-empty")
    entries: list[MigrationManifestEntry] = []
    expected_keys = {
        "version",
        "filename",
        "sha256",
        "transactional",
        "minimum_app_release",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise ValueError("migration manifest entry shape is invalid")
        version = record["version"]
        filename = record["filename"]
        digest = record["sha256"]
        transactional = record["transactional"]
        minimum_release = record["minimum_app_release"]
        if not isinstance(version, int) or version < 1:
            raise ValueError("migration manifest version is invalid")
        if (
            not isinstance(filename, str)
            or len(filename) <= 8
            or not filename.startswith(f"{version:03d}_")
            or not filename.endswith(".sql")
        ):
            raise ValueError("migration manifest filename is invalid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(value not in "0123456789abcdef" for value in digest)
        ):
            raise ValueError("migration manifest checksum is invalid")
        if transactional is not True:
            raise ValueError(
                "this release supports transactional migrations only"
            )
        if not isinstance(minimum_release, str) or not minimum_release:
            raise ValueError("migration minimum_app_release is invalid")
        entries.append(
            MigrationManifestEntry(
                version=version,
                filename=filename,
                sha256=digest,
                transactional=transactional,
                minimum_app_release=minimum_release,
            )
        )
    versions = [entry.version for entry in entries]
    if versions != list(range(1, target_version + 1)):
        raise ValueError(
            "migration manifest must declare an ordered contiguous release"
        )
    if entries[-1].version != target_version:
        raise ValueError("migration manifest target does not match its entries")
    return MigrationReleaseManifest(
        schema_version=str(payload["schema_version"]),
        target_version=target_version,
        migrations=tuple(entries),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


MIGRATION_RELEASE = load_migration_manifest()
LATEST_SCHEMA_VERSION = MIGRATION_RELEASE.target_version
MIGRATION_MANIFEST_SHA256 = MIGRATION_RELEASE.manifest_sha256
EXPECTED_MIGRATION_IDENTITIES = tuple(
    migration_identity(entry) for entry in MIGRATION_RELEASE.migrations
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


__all__ = [
    "EXPECTED_MIGRATION_IDENTITIES",
    "LATEST_SCHEMA_VERSION",
    "MIGRATION_MANIFEST_PATH",
    "MIGRATION_MANIFEST_SHA256",
    "MIGRATION_RELEASE",
    "MigrationManifestEntry",
    "MigrationReleaseManifest",
    "POSTGRES_BASELINE_SQL",
    "SCHEMA_VERSION",
    "load_migration_manifest",
    "migration_identity",
    "split_sql_statements",
]
