"""Fail-closed key-reference adapter for backend identity and encryption.

Production accepts environment- or file-backed references.  Deterministic
material is available only to explicit test/development profiles and is useful
for reproducible replay fixtures; the reference, never the secret, appears in
dependency manifests.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sleepagent.backend_settings import DeploymentMode


class BackendKeyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActorKeyMaterial:
    key_id: str
    public_key_pem: bytes
    private_key: Ed25519PrivateKey | None = None

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(self.public_key_pem).hexdigest()


class BackendKeyProvider:
    def __init__(
        self,
        deployment_mode: DeploymentMode,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.deployment_mode = deployment_mode
        self.environment = os.environ if environment is None else environment

    def secret(self, reference: str, *, purpose: str, minimum_bytes: int) -> bytes:
        value = self._resolve(reference, purpose=purpose)
        if len(value) < minimum_bytes:
            raise BackendKeyError(f"{purpose} key material is too short")
        return value

    def actor_key(self, reference: str, *, key_id: str) -> ActorKeyMaterial:
        if reference.startswith("test:"):
            self._require_nonproduction_test_reference(reference)
            private_key = Ed25519PrivateKey.from_private_bytes(
                hashlib.sha256(
                    f"sleepagent:actor-assertion:{reference}".encode("utf-8")
                ).digest()
            )
            public_pem = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return ActorKeyMaterial(
                key_id=key_id,
                public_key_pem=public_pem,
                private_key=private_key,
            )
        public_pem = self._resolve(reference, purpose="actor verification")
        try:
            key = serialization.load_pem_public_key(public_pem)
        except (TypeError, ValueError) as exc:
            raise BackendKeyError("actor verification key is not valid PEM") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise BackendKeyError("actor verification key must be Ed25519")
        canonical = key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return ActorKeyMaterial(key_id=key_id, public_key_pem=canonical)

    def encryption_key(self, reference: str) -> bytes:
        raw = self._resolve(reference, purpose="encryption")
        if reference.startswith("test:"):
            return hashlib.sha256(raw).digest()
        try:
            decoded = base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise BackendKeyError("encryption key must be canonical base64") from exc
        if len(decoded) != 32:
            raise BackendKeyError("encryption key must decode to 32 bytes")
        if base64.b64encode(decoded) != raw.strip():
            raise BackendKeyError("encryption key must use canonical base64")
        return decoded

    def _resolve(self, reference: str, *, purpose: str) -> bytes:
        if reference.startswith("test:"):
            self._require_nonproduction_test_reference(reference)
            return hashlib.sha256(
                f"sleepagent:{purpose}:{reference}".encode("utf-8")
            ).digest()
        if reference.startswith("env:"):
            name = reference.removeprefix("env:")
            if not name or name not in self.environment:
                raise BackendKeyError(f"{purpose} environment reference is unavailable")
            value = self.environment[name].encode("utf-8")
            if not value:
                raise BackendKeyError(f"{purpose} environment secret is empty")
            return value
        if reference.startswith("file:"):
            raw_path = reference.removeprefix("file:")
            path = Path(raw_path)
            if not path.is_absolute() or path.is_symlink():
                raise BackendKeyError(f"{purpose} file reference must be absolute and not a symlink")
            try:
                value = path.read_bytes()
            except OSError as exc:
                raise BackendKeyError(f"{purpose} file reference is unavailable") from exc
            if not value:
                raise BackendKeyError(f"{purpose} key file is empty")
            return value
        raise BackendKeyError(
            f"{purpose} reference must use env:, file:, or explicit test: scheme"
        )

    def _require_nonproduction_test_reference(self, reference: str) -> None:
        if self.deployment_mode == DeploymentMode.PRODUCTION:
            raise BackendKeyError(
                f"deterministic key reference {reference!r} is forbidden in production"
            )


__all__ = [
    "ActorKeyMaterial",
    "BackendKeyError",
    "BackendKeyProvider",
]
