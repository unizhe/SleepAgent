"""Configured encryption policy for immutable raw-ingress payloads."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken


RAW_ENCRYPTION_KEY_ENV = "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY"
RAW_ENCRYPTION_KEY_ID_ENV = "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY_ID"
RAW_RETENTION_SECONDS_ENV = "SLEEP_DOMAIN_RAW_RETENTION_SECONDS"


class RawPayloadConfigurationError(RuntimeError):
    pass


class RawPayloadDecryptionError(ValueError):
    pass


@dataclass(frozen=True)
class EncryptedRawPayload:
    ciphertext: bytes
    key_id: str
    encrypted_at: datetime


@dataclass(frozen=True)
class RawPayloadEncryptionPolicy:
    """Explicit key and retention policy; there are no insecure defaults."""

    key_id: str
    key: bytes
    retention_period: timedelta
    production: bool
    _fernet: Fernet = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.key_id.strip():
            raise RawPayloadConfigurationError("raw encryption key_id is required")
        if self.retention_period <= timedelta(0):
            raise RawPayloadConfigurationError(
                "raw retention period must be explicitly positive"
            )
        try:
            fernet = Fernet(self.key)
        except (TypeError, ValueError) as exc:
            raise RawPayloadConfigurationError(
                "raw encryption key must be a valid Fernet key"
            ) from exc
        object.__setattr__(self, "_fernet", fernet)

    @classmethod
    def from_environment(
        cls,
        *,
        production: bool,
        environ: Mapping[str, str] | None = None,
    ) -> "RawPayloadEncryptionPolicy":
        values = os.environ if environ is None else environ
        key_text = values.get(RAW_ENCRYPTION_KEY_ENV)
        key_id = values.get(RAW_ENCRYPTION_KEY_ID_ENV)
        retention_text = values.get(RAW_RETENTION_SECONDS_ENV)
        missing = [
            name
            for name, value in (
                (RAW_ENCRYPTION_KEY_ENV, key_text),
                (RAW_ENCRYPTION_KEY_ID_ENV, key_id),
                (RAW_RETENTION_SECONDS_ENV, retention_text),
            )
            if not value
        ]
        if missing:
            mode = "production" if production else "configured local/test"
            raise RawPayloadConfigurationError(
                f"{mode} raw persistence requires: {', '.join(missing)}"
            )
        try:
            retention_seconds = int(str(retention_text))
        except ValueError as exc:
            raise RawPayloadConfigurationError(
                f"{RAW_RETENTION_SECONDS_ENV} must be an integer"
            ) from exc
        return cls(
            key_id=str(key_id),
            key=str(key_text).encode("ascii"),
            retention_period=timedelta(seconds=retention_seconds),
            production=production,
        )

    def validate_retention(
        self,
        *,
        received_at: datetime,
        retention_until: datetime,
    ) -> None:
        expected = received_at + self.retention_period
        if retention_until != expected:
            raise RawPayloadConfigurationError(
                "retention_until must exactly match the configured retention policy"
            )

    def encrypt(
        self,
        *,
        raw_ingress_record_id: str,
        namespace_id: str,
        payload: bytes,
        encrypted_at: datetime,
    ) -> EncryptedRawPayload:
        envelope = json.dumps(
            {
                "raw_ingress_record_id": raw_ingress_record_id,
                "namespace_id": namespace_id,
                "payload_base64": base64.b64encode(payload).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EncryptedRawPayload(
            ciphertext=self._fernet.encrypt(envelope),
            key_id=self.key_id,
            encrypted_at=encrypted_at,
        )

    def decrypt(
        self,
        *,
        raw_ingress_record_id: str,
        namespace_id: str,
        ciphertext: bytes,
        key_id: str,
    ) -> bytes:
        if key_id != self.key_id:
            raise RawPayloadDecryptionError(
                f"raw payload requires unavailable key_id: {key_id}"
            )
        try:
            envelope = json.loads(self._fernet.decrypt(ciphertext))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RawPayloadDecryptionError(
                "raw payload authentication/decryption failed"
            ) from exc
        if (
            envelope.get("raw_ingress_record_id") != raw_ingress_record_id
            or envelope.get("namespace_id") != namespace_id
        ):
            raise RawPayloadDecryptionError(
                "raw payload encrypted context does not match the requested record"
            )
        try:
            return base64.b64decode(
                str(envelope["payload_base64"]),
                validate=True,
            )
        except (KeyError, ValueError) as exc:
            raise RawPayloadDecryptionError(
                "raw payload envelope is malformed"
            ) from exc


__all__ = [
    "EncryptedRawPayload",
    "RAW_ENCRYPTION_KEY_ENV",
    "RAW_ENCRYPTION_KEY_ID_ENV",
    "RAW_RETENTION_SECONDS_ENV",
    "RawPayloadConfigurationError",
    "RawPayloadDecryptionError",
    "RawPayloadEncryptionPolicy",
]
