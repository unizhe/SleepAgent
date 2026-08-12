"""Bounded replay retention key-envelope and crypto-shred adapters."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope


RAW_RETENTION_CLASS = "replay_raw_short_v1"
RAW_RETENTION_DOMAIN = "raw"
WRAPPING_ALGORITHM = "AES-256-GCM-local-envelope.v1"


class RetentionError(RuntimeError):
    pass


class RetentionKeyDestroyed(RetentionError):
    code = "key_destroyed"

    def __init__(self) -> None:
        super().__init__(self.code)


class RetentionKeyEnvelopePort(Protocol):
    key_id: str
    wrapping_algorithm: str

    def generate_data_key(self) -> bytes: ...

    def wrap_data_key(self, data_key: bytes, *, aad: bytes) -> bytes: ...

    def unwrap_data_key(self, wrapped_data_key: bytes, *, aad: bytes) -> bytes: ...

    def destroy_wrapped_key(self, wrapped_data_key: bytes, *, aad: bytes) -> str: ...


class LocalTestRetentionKeyEnvelope:
    """Stateless local KEK adapter proving the KMS-facing port in replay only."""

    wrapping_algorithm = WRAPPING_ALGORITHM

    def __init__(self, kek: bytes, *, key_id: str) -> None:
        if len(kek) != 32:
            raise ValueError("retention KEK must contain exactly 32 bytes")
        if not key_id.strip():
            raise ValueError("retention KEK key_id is required")
        self._aead = AESGCM(kek)
        self.key_id = key_id

    def generate_data_key(self) -> bytes:
        return secrets.token_bytes(32)

    def wrap_data_key(self, data_key: bytes, *, aad: bytes) -> bytes:
        if len(data_key) != 32:
            raise ValueError("retention DEK must contain exactly 32 bytes")
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, data_key, aad)

    def unwrap_data_key(self, wrapped_data_key: bytes, *, aad: bytes) -> bytes:
        if len(wrapped_data_key) < 60:
            raise RetentionError("wrapped retention DEK is truncated")
        try:
            value = self._aead.decrypt(
                wrapped_data_key[:12], wrapped_data_key[12:], aad
            )
        except InvalidTag as exc:
            raise RetentionError("wrapped retention DEK authentication failed") from exc
        if len(value) != 32:
            raise RetentionError("unwrapped retention DEK has invalid length")
        return value

    def destroy_wrapped_key(self, wrapped_data_key: bytes, *, aad: bytes) -> str:
        # The local adapter is intentionally stateless.  The durable linearization
        # point is the fenced database transition that removes wrapped_dek.
        self.unwrap_data_key(wrapped_data_key, aad=aad)
        return "local-envelope:destroy-authorized:" + hashlib.sha256(
            aad + wrapped_data_key
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class RetentionDataKey:
    generation: int
    data_key: bytes
    wrapped_data_key: bytes
    key_id: str
    wrapping_algorithm: str


class PostgresRetentionKeyCoordinator:
    """Coordinate one subject/domain DEK with raw bindings in the caller UoW."""

    def __init__(self, envelope: RetentionKeyEnvelopePort) -> None:
        self.envelope = envelope

    def ensure_raw_key(self, connection: Any, scope: UowScope) -> RetentionDataKey:
        if scope.process_role != "worker" or scope.subject_id is None:
            raise ValueError("retention key creation requires worker subject scope")
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT generation, kek_key_id, wrapping_algorithm, wrapped_dek, "
                "dek_sha256 FROM public.backend_retention_deks "
                "WHERE namespace_id = %s AND data_mode = %s AND subject_id = %s "
                "AND namespace_generation = %s "
                "AND run_id IS NOT DISTINCT FROM %s "
                "AND arm_id IS NOT DISTINCT FROM %s "
                "AND retention_domain = 'raw' AND status = 'active' FOR UPDATE",
                (
                    scope.namespace_id,
                    scope.data_mode,
                    scope.subject_id,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                if (
                    str(row[1]) != self.envelope.key_id
                    or str(row[2]) != self.envelope.wrapping_algorithm
                    or row[3] is None
                ):
                    raise RetentionError("active raw DEK envelope is unavailable")
                generation = int(row[0])
                wrapped = bytes(row[3])
                data_key = self.envelope.unwrap_data_key(
                    wrapped, aad=_dek_aad(scope, generation)
                )
                if hashlib.sha256(data_key).hexdigest() != str(row[4]):
                    raise RetentionError("active raw DEK digest drifted")
                return RetentionDataKey(
                    generation=generation,
                    data_key=data_key,
                    wrapped_data_key=wrapped,
                    key_id=self.envelope.key_id,
                    wrapping_algorithm=self.envelope.wrapping_algorithm,
                )
            cursor.execute(
                "SELECT COALESCE(MAX(generation) + 1, %s) "
                "FROM public.backend_retention_deks "
                "WHERE namespace_id = %s AND data_mode = %s AND subject_id = %s "
                "AND namespace_generation = %s "
                "AND retention_domain = 'raw'",
                (
                    (scope.namespace_generation - 1) * 1_000_000 + 1,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.subject_id,
                    scope.namespace_generation,
                ),
            )
            generation_row = cursor.fetchone()
            if generation_row is None:
                raise RetentionError("raw DEK generation is unavailable")
            generation = int(generation_row[0])
            data_key = self.envelope.generate_data_key()
            wrapped = self.envelope.wrap_data_key(
                data_key, aad=_dek_aad(scope, generation)
            )
            cursor.execute(
                "INSERT INTO public.backend_retention_deks ("
                "namespace_id, data_mode, namespace_generation, run_id, arm_id, "
                "subject_id, retention_domain, generation, kek_key_id, "
                "wrapping_algorithm, wrapped_dek, dek_sha256, status) VALUES ("
                "%s, %s, %s, %s, %s, %s, 'raw', %s, %s, %s, %s, %s, 'active')",
                (
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    generation,
                    self.envelope.key_id,
                    self.envelope.wrapping_algorithm,
                    wrapped,
                    hashlib.sha256(data_key).hexdigest(),
                ),
            )
        finally:
            cursor.close()
        return RetentionDataKey(
            generation=generation,
            data_key=data_key,
            wrapped_data_key=wrapped,
            key_id=self.envelope.key_id,
            wrapping_algorithm=self.envelope.wrapping_algorithm,
        )

    def bind_raw(
        self,
        connection: Any,
        scope: UowScope,
        *,
        raw_ingress_record_id: str,
        generation: int,
        expires_at: datetime,
        retention_binding_id: str,
        retention_job_id: str,
    ) -> None:
        if scope.subject_id is None:
            raise ValueError("raw retention binding requires subject scope")
        semantic_key = _sha256(
            {
                "job_kind": "scheduled_expiry",
                "namespace_id": scope.namespace_id,
                "subject_id": scope.subject_id,
                "retention_domain": RAW_RETENTION_DOMAIN,
                "dek_generation": generation,
            }
        )
        authorization = _workload_snapshot(scope)
        binding = {
            "schema_version": "retention_binding.v1",
            "retention_domain": RAW_RETENTION_DOMAIN,
            "dek_generation": generation,
            "resource_type": "RawIngressRecord",
            "expires_at": expires_at.isoformat(),
            "legal_hold": False,
        }
        job = {
            "schema_version": "retention_job.v1",
            "job_kind": "scheduled_expiry",
            "retention_domain": RAW_RETENTION_DOMAIN,
            "dek_generation": generation,
            "policy_version": "bounded-replay-raw.v1",
            "authorization_snapshot": authorization,
        }
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO public.backend_retention_bindings ("
                "retention_binding_id, namespace_id, data_mode, "
                "namespace_generation, run_id, arm_id, subject_id, "
                "retention_domain, dek_generation, retention_class, "
                "resource_type, resource_id, expires_at, legal_hold, "
                "binding_json) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, 'raw', %s, %s, "
                "'RawIngressRecord', %s, %s, FALSE, %s::jsonb)",
                (
                    retention_binding_id,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    generation,
                    RAW_RETENTION_CLASS,
                    raw_ingress_record_id,
                    expires_at,
                    _json(binding),
                ),
            )
            cursor.execute(
                "INSERT INTO public.backend_retention_jobs ("
                "retention_job_id, namespace_id, data_mode, namespace_generation, "
                "run_id, arm_id, subject_id, retention_domain, dek_generation, "
                "semantic_key, job_kind, status, priority, available_at, "
                "authorization_snapshot_json, job_json) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, 'raw', %s, %s, "
                "'scheduled_expiry', 'pending', 20, %s, %s::jsonb, %s::jsonb) "
                "ON CONFLICT (namespace_id, data_mode, semantic_key) DO UPDATE "
                "SET available_at = GREATEST("
                "backend_retention_jobs.available_at, EXCLUDED.available_at), "
                "updated_at = clock_timestamp() "
                "WHERE backend_retention_jobs.status IN ('pending', 'retry')",
                (
                    retention_job_id,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    generation,
                    semantic_key,
                    expires_at,
                    _json(authorization),
                    _json(job),
                ),
            )
        finally:
            cursor.close()

    def decrypt_raw(
        self,
        *,
        scope: UowScope,
        generation: int,
        wrapped_data_key: bytes | None,
        dek_sha256: str,
        status: str,
        encrypted_payload: bytes,
        aad: bytes,
    ) -> bytes:
        if status == "shredded" or wrapped_data_key is None:
            raise RetentionKeyDestroyed()
        data_key = self.envelope.unwrap_data_key(
            wrapped_data_key, aad=_dek_aad(scope, generation)
        )
        if hashlib.sha256(data_key).hexdigest() != dek_sha256:
            raise RetentionError("raw DEK digest drifted")
        return _decrypt_payload(data_key, encrypted_payload, aad=aad)


class PostgresRawRetentionReader:
    """Canonical v2 raw read port with stable crypto-shred semantics."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        keys: PostgresRetentionKeyCoordinator,
    ) -> None:
        self.uow_factory = uow_factory
        self.keys = keys

    def read(self, scope: UowScope, *, raw_ingress_record_id: str) -> bytes:
        if (
            scope.process_role != "worker"
            or scope.subject_id is None
            or not raw_ingress_record_id.strip()
        ):
            raise ValueError("raw retention read requires exact worker scope")
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT raw.encrypted_payload, "
                    "raw.pre_normalization_payload_sha256, raw.dek_generation, "
                    "dek.wrapped_dek, dek.dek_sha256, dek.status "
                    "FROM public.sleep_domain_raw_inbox AS raw "
                    "JOIN public.backend_retention_deks AS dek "
                    "ON dek.namespace_id = raw.namespace_id "
                    "AND dek.data_mode = raw.data_mode "
                    "AND dek.subject_id = raw.retention_subject_id "
                    "AND dek.retention_domain = raw.retention_domain "
                    "AND dek.generation = raw.dek_generation "
                    "WHERE raw.raw_ingress_record_id = %s "
                    "AND raw.namespace_id = %s AND raw.data_mode = %s "
                    "AND raw.namespace_generation = %s "
                    "AND raw.run_id IS NOT DISTINCT FROM %s "
                    "AND raw.arm_id IS NOT DISTINCT FROM %s "
                    "AND raw.subject_id = %s "
                    "AND raw.encryption_protocol_version >= 2",
                    (
                        raw_ingress_record_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise LookupError("raw_record_not_found")
            raw = self.keys.decrypt_raw(
                scope=scope,
                generation=int(row[2]),
                wrapped_data_key=None if row[3] is None else bytes(row[3]),
                dek_sha256=str(row[4]),
                status=str(row[5]),
                encrypted_payload=bytes(row[0]),
                aad=_raw_aad(scope, raw_ingress_record_id),
            )
            if hashlib.sha256(raw).hexdigest() != str(row[1]):
                raise RetentionError("raw payload digest drifted")
            uow.commit()
        return raw


def encrypt_raw_payload(data_key: bytes, payload: bytes, *, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(data_key).encrypt(nonce, payload, b"sleepagent-replay-raw.v2" + aad)


def _decrypt_payload(data_key: bytes, payload: bytes, *, aad: bytes) -> bytes:
    if len(payload) < 29:
        raise RetentionError("encrypted raw payload is truncated")
    try:
        return AESGCM(data_key).decrypt(
            payload[:12], payload[12:], b"sleepagent-replay-raw.v2" + aad
        )
    except InvalidTag as exc:
        raise RetentionError("encrypted raw payload authentication failed") from exc


def _dek_aad(scope: UowScope, generation: int) -> bytes:
    return _json(
        {
            "schema_version": "retention_dek_aad.v1",
            "namespace_id": scope.namespace_id,
            "data_mode": scope.data_mode,
            "subject_id": scope.subject_id,
            "retention_domain": RAW_RETENTION_DOMAIN,
            "generation": generation,
        }
    ).encode()


def _raw_aad(scope: UowScope, raw_ingress_record_id: str) -> bytes:
    return "\0".join(
        (
            scope.namespace_id,
            str(scope.namespace_generation),
            scope.data_mode,
            scope.run_id or "",
            scope.arm_id or "",
            scope.subject_id or "",
            raw_ingress_record_id,
        )
    ).encode("utf-8")


def _workload_snapshot(scope: UowScope) -> dict[str, Any]:
    return {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": "retention",
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


__all__ = [
    "LocalTestRetentionKeyEnvelope",
    "PostgresRetentionKeyCoordinator",
    "PostgresRawRetentionReader",
    "RAW_RETENTION_CLASS",
    "RAW_RETENTION_DOMAIN",
    "RetentionDataKey",
    "RetentionError",
    "RetentionKeyDestroyed",
    "RetentionKeyEnvelopePort",
    "encrypt_raw_payload",
]
