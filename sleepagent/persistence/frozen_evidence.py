"""Reusable integrity checks for repository-external frozen evidence.

The module deliberately does not create database backups or orchestrate a
restore.  It preserves the stable security and semantic-comparison contracts
that a deployment-specific backup/restore procedure must satisfy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEY = re.compile(
    r"(client[_-]?secret|access[_-]?token|api[_-]?key|password|"
    r"database[_-]?dsn|^dsn$)",
    re.IGNORECASE,
)
_TEMPORARY_ROOTS = (Path("/tmp"), Path("/var/tmp"))

FROZEN_RESTORE_SEMANTIC_FIELDS = (
    "episode_semantic_sha256",
    "membership_count",
    "membership_set_sha256",
    "quality_state",
    "episode_canonical_sha256",
    "canonical_observation_count",
    "canonical_semantic_sha256",
    "provenance_sha256",
    "raw_evidence_count",
    "raw_evidence_sha256",
    "device_binding_version",
    "device_binding_reference_sha256",
    "device_binding_semantic_sha256",
    "device_binding_provenance_sha256",
    "device_binding_audit_count",
    "subject_epoch_sha256",
    "device_binding_identity_fingerprints",
    "schema_version_number",
    "migration_ledger_sha256",
)


class FrozenEvidenceError(RuntimeError):
    """A private-evidence integrity or restore-semantics check failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical, credential-key-free manifest with an integrity hash."""

    sealed = dict(payload)
    sealed.pop("manifest_sha256", None)
    _assert_no_credential_keys(sealed)
    sealed["manifest_sha256"] = _canonical_sha256(sealed)
    return sealed


def validate_sealed_manifest(payload: Mapping[str, Any], schema: str) -> None:
    if payload.get("schema_version") != schema:
        raise FrozenEvidenceError("sealed evidence schema is unsupported")
    supplied = payload.get("manifest_sha256")
    if not isinstance(supplied, str) or not _SHA256.fullmatch(supplied):
        raise FrozenEvidenceError("sealed evidence integrity field is invalid")
    unsealed = dict(payload)
    unsealed.pop("manifest_sha256", None)
    _assert_no_credential_keys(unsealed)
    if _canonical_sha256(unsealed) != supplied:
        raise FrozenEvidenceError("sealed evidence integrity check failed")


def ensure_private_archive_root(
    root: Path,
    *,
    repository_root: Path,
    create: bool = False,
) -> None:
    """Require a persistent owner-only archive outside Git and temp roots."""

    if not root.is_absolute():
        raise FrozenEvidenceError("evidence archive root must be absolute")
    resolved = root.resolve(strict=False)
    repository = repository_root.resolve()
    if resolved == repository or repository in resolved.parents:
        raise FrozenEvidenceError("evidence archive root must remain outside Git")
    for temporary in _TEMPORARY_ROOTS:
        if resolved == temporary or temporary in resolved.parents:
            raise FrozenEvidenceError("evidence archive root must not be temporary")
    if root.exists() and root.is_symlink():
        raise FrozenEvidenceError("evidence archive root must not be a symlink")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
    if not root.is_dir():
        raise FrozenEvidenceError("evidence archive root is unavailable")
    _assert_owner_mode(root, expected=0o700)


def validate_private_tree(root: Path) -> None:
    """Require owner-only directories/files and reject symlinks."""

    if not root.is_dir():
        raise FrozenEvidenceError("private evidence tree is unavailable")
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise FrozenEvidenceError("private evidence tree contains a symlink")
        _assert_owner_mode(path, expected=0o700 if path.is_dir() else 0o600)


def assert_frozen_restore_semantics(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    """Compare every production-relevant frozen NightEpisode semantic field."""

    missing_expected = tuple(
        field for field in FROZEN_RESTORE_SEMANTIC_FIELDS if field not in expected
    )
    if missing_expected:
        raise FrozenEvidenceError(
            "frozen manifest omits semantic fields: " + ", ".join(missing_expected)
        )
    additional_fields = tuple(
        sorted(set(expected) - set(FROZEN_RESTORE_SEMANTIC_FIELDS))
    )
    for field in (*FROZEN_RESTORE_SEMANTIC_FIELDS, *additional_fields):
        if field not in actual:
            raise FrozenEvidenceError(f"restored semantic field is missing: {field}")
        if actual[field] != expected[field]:
            raise FrozenEvidenceError(f"restored semantic field drifted: {field}")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_no_credential_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _FORBIDDEN_KEY.search(str(key)):
                raise FrozenEvidenceError("manifest contains a credential-shaped key")
            _assert_no_credential_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_credential_keys(nested)


def _assert_owner_mode(path: Path, *, expected: int) -> None:
    details = path.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != expected:
        raise FrozenEvidenceError(
            f"private evidence path must be owner-only {expected:04o}"
        )


__all__ = [
    "FROZEN_RESTORE_SEMANTIC_FIELDS",
    "FrozenEvidenceError",
    "assert_frozen_restore_semantics",
    "ensure_private_archive_root",
    "seal_manifest",
    "sha256_file",
    "validate_private_tree",
    "validate_sealed_manifest",
]
