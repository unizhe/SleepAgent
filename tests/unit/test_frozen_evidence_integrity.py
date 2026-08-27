from __future__ import annotations

import json
from pathlib import Path

import pytest

from sleepagent.persistence.frozen_evidence import (
    FROZEN_RESTORE_SEMANTIC_FIELDS,
    FrozenEvidenceError,
    assert_frozen_restore_semantics,
    ensure_private_archive_root,
    seal_manifest,
    sha256_file,
    validate_private_tree,
    validate_sealed_manifest,
)


def test_archive_root_rejects_temporary_location(tmp_path: Path) -> None:
    with pytest.raises(FrozenEvidenceError, match="temporary"):
        ensure_private_archive_root(
            tmp_path / "evidence",
            repository_root=tmp_path / "repository",
            create=True,
        )


def test_archive_root_rejects_repository_destination(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    destination = repository / "private"
    with pytest.raises(FrozenEvidenceError, match="outside Git"):
        ensure_private_archive_root(
            destination,
            repository_root=repository,
            create=True,
        )


def test_sealed_manifest_detects_tamper() -> None:
    payload = seal_manifest(
        {
            "schema_version": "test.v1",
            "database": {"backup_sha256": "a" * 64},
        }
    )
    validate_sealed_manifest(payload, "test.v1")
    changed = dict(payload)
    changed["database"] = {"backup_sha256": "b" * 64}
    with pytest.raises(FrozenEvidenceError, match="integrity"):
        validate_sealed_manifest(changed, "test.v1")


@pytest.mark.parametrize(
    "credential_key",
    (
        "ClientSecret",
        "client_secret",
        "access_token",
        "api-key",
        "password",
        "database_dsn",
        "dsn",
    ),
)
def test_sealed_manifest_rejects_credential_shaped_keys(
    credential_key: str,
) -> None:
    with pytest.raises(FrozenEvidenceError, match="credential-shaped"):
        seal_manifest({"schema_version": "test.v1", credential_key: "never"})


def test_private_tree_requires_owner_only_modes(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    artifact = root / "artifact"
    artifact.write_bytes(b"durable")
    artifact.chmod(0o600)
    validate_private_tree(root)
    artifact.chmod(0o644)
    with pytest.raises(FrozenEvidenceError, match="owner-only"):
        validate_private_tree(root)


def test_backup_hash_detects_mutation(tmp_path: Path) -> None:
    artifact = tmp_path / "database.dump"
    artifact.write_bytes(b"backup-v1")
    first = sha256_file(artifact)
    artifact.write_bytes(b"backup-v2")
    assert sha256_file(artifact) != first


def _snapshot() -> dict[str, object]:
    return {
        field: index
        for index, field in enumerate(FROZEN_RESTORE_SEMANTIC_FIELDS, start=1)
    }


@pytest.mark.parametrize("field", FROZEN_RESTORE_SEMANTIC_FIELDS)
def test_restore_snapshot_compares_every_frozen_semantic_field(field: str) -> None:
    expected = _snapshot()
    assert_frozen_restore_semantics(expected, dict(expected))
    changed = dict(expected)
    changed[field] = -1
    with pytest.raises(FrozenEvidenceError, match=field):
        assert_frozen_restore_semantics(expected, changed)


def test_restore_snapshot_rejects_incomplete_manifest() -> None:
    expected = _snapshot()
    expected.pop(FROZEN_RESTORE_SEMANTIC_FIELDS[-1])
    with pytest.raises(FrozenEvidenceError, match="omits semantic fields"):
        assert_frozen_restore_semantics(expected, _snapshot())


def test_manifest_serialization_contains_no_credentials() -> None:
    payload = seal_manifest(
        {
            "schema_version": "test.v1",
            "episode": {"semantic_sha256": "a" * 64},
            "database": {"identity_sha256": "b" * 64},
        }
    )
    serialized = json.dumps(payload, sort_keys=True).lower()
    assert "clientsecret" not in serialized
    assert "access_token" not in serialized
    assert "api_key" not in serialized
