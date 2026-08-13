# 本模块执行有界保留、密钥封装与可审计的加密擦除流程。
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

# durable retention queue handler 与 retention 加密边界共置。

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from sleepagent.config import BackendKeyProvider
from sleepagent.config import (
    DataMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.workers.retention import (
    LocalTestRetentionKeyEnvelope,
    RAW_RETENTION_CLASS,
    RAW_RETENTION_DOMAIN,
    RetentionKeyEnvelopePort,
)
from sleepagent.domain.episodes import UUID7Generator
from sleepagent.workers.runtime import (
    B3ClaimInvariantError,
    exact_worker_scope,
    worker_uow_factory,
)
from sleepagent.workers.runtime import (
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
)


RETENTION_QUEUE = "retention"
RAW_EXPIRY_POLICY_VERSION = "bounded-replay-raw.v1"
DEMO_RESET_QUEUE = "demo_reset"
NORMALIZATION_TERMINAL_STATES = frozenset(
    {"succeeded", "dead_letter", "quarantined"}
)


class RetentionWorkerError(RuntimeError):
    pass


class RetentionLeaseLost(RetentionWorkerError):
    pass


@dataclass(frozen=True, slots=True)
class _RawExpiryPlan:
    wrapped_dek: bytes
    configuration_sha256: str
    binding_count: int
    raw_count: int


class RawRetentionWorkHandler:
    """Retire and shred one due raw-domain DEK under its durable fence."""

    def __init__(
        self,
        *,
        envelope: RetentionKeyEnvelopePort,
        id_generator: UUID7Generator | None = None,
    ) -> None:
        self.envelope = envelope
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _retention_scope(context)
            plan = self._retire_when_ready(context, scope=scope)
            if plan is None:
                return WorkResult(
                    disposition=WorkDisposition.RETRYABLE,
                    error_code="raw_retention_not_ready",
                    retry_after_seconds=1,
                )
            evidence = self.envelope.destroy_wrapped_key(
                plan.wrapped_dek,
                aad=_dek_aad(scope, _dek_generation(context)),
            )
            result = self._commit_shred(
                context,
                scope=scope,
                plan=plan,
                destruction_evidence=evidence,
            )
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=result,
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except RetentionLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="retention_lease_lost_reconciliation_required",
            )
        except (B3ClaimInvariantError, RetentionWorkerError, TypeError, ValueError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="raw_retention_invariant_violation",
            )

    def _retire_when_ready(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
    ) -> _RawExpiryPlan | None:
        claim = context.claim
        generation = _dek_generation(context)
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT job.job_kind, job.available_at <= clock_timestamp(), "
                    "dek.status, dek.wrapped_dek, dek.kek_key_id, "
                    "dek.wrapping_algorithm, class.configuration_sha256 "
                    "FROM public.backend_retention_jobs AS job "
                    "JOIN public.backend_retention_deks AS dek "
                    "ON dek.namespace_id = job.namespace_id "
                    "AND dek.data_mode = job.data_mode "
                    "AND dek.subject_id = job.subject_id "
                    "AND dek.retention_domain = job.retention_domain "
                    "AND dek.generation = job.dek_generation "
                    "JOIN public.backend_retention_classes AS class "
                    "ON class.retention_class = %s AND class.active = TRUE "
                    "WHERE job.retention_job_id = %s "
                    "AND job.status = 'running' "
                    "AND job.namespace_generation = %s "
                    "AND job.lease_generation = %s "
                    "AND job.fencing_token = %s "
                    "AND job.worker_instance = %s "
                    "AND job.lease_expires_at > clock_timestamp() "
                    "FOR UPDATE OF job, dek",
                    (
                        RAW_RETENTION_CLASS,
                        claim.work_id,
                        claim.namespace_generation,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RetentionLeaseLost("retention fence was lost")
                if (
                    str(row[0]) != "scheduled_expiry"
                    or str(row[4]) != self.envelope.key_id
                    or str(row[5]) != self.envelope.wrapping_algorithm
                ):
                    raise RetentionWorkerError("raw retention policy drifted")
                if str(row[2]) == "shredded":
                    raise RetentionWorkerError("shredded DEK retained runnable work")
                if row[3] is None:
                    raise RetentionWorkerError("raw wrapped DEK is unavailable")

                cursor.execute(
                    "SELECT count(*), "
                    "COALESCE(bool_and(expires_at <= clock_timestamp()), FALSE), "
                    "COALESCE(bool_and(legal_hold = FALSE), FALSE) "
                    "FROM public.backend_retention_bindings "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND subject_id = %s AND retention_domain = 'raw' "
                    "AND dek_generation = %s",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                    ),
                )
                binding_row = cursor.fetchone()
                if binding_row is None:
                    raise RetentionWorkerError("raw retention bindings unavailable")
                binding_count = int(binding_row[0])

                cursor.execute(
                    "SELECT count(*), COALESCE(bool_and(work.status = ANY(%s)), "
                    "FALSE) FROM public.backend_retention_bindings AS binding "
                    "JOIN public.sleep_domain_normalization_work AS work "
                    "ON work.namespace_id = binding.namespace_id "
                    "AND work.data_mode = binding.data_mode "
                    "AND work.raw_ingress_record_id = binding.resource_id "
                    "WHERE binding.namespace_id = %s "
                    "AND binding.data_mode = %s AND binding.subject_id = %s "
                    "AND binding.retention_domain = 'raw' "
                    "AND binding.dek_generation = %s "
                    "AND binding.resource_type = 'RawIngressRecord'",
                    (
                        sorted(NORMALIZATION_TERMINAL_STATES),
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                    ),
                )
                normalized_row = cursor.fetchone()
                if normalized_row is None:
                    raise RetentionWorkerError("normalization state unavailable")
                raw_count = int(normalized_row[0])
                ready = (
                    row[1] is True
                    and binding_count > 0
                    and raw_count == binding_count
                    and binding_row[1] is True
                    and binding_row[2] is True
                    and normalized_row[1] is True
                )
                if not ready:
                    uow.commit()
                    return None
                if str(row[2]) == "active":
                    cursor.execute(
                        "UPDATE public.backend_retention_deks "
                        "SET status = 'retired', retired_at = clock_timestamp() "
                        "WHERE namespace_id = %s AND data_mode = %s "
                        "AND subject_id = %s AND retention_domain = 'raw' "
                        "AND generation = %s AND status = 'active'",
                        (
                            scope.namespace_id,
                            scope.data_mode,
                            scope.subject_id,
                            generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RetentionLeaseLost("raw DEK retire CAS was lost")
            finally:
                cursor.close()
            uow.commit()
        return _RawExpiryPlan(
            wrapped_dek=bytes(row[3]),
            configuration_sha256=str(row[6]),
            binding_count=binding_count,
            raw_count=raw_count,
        )

    def _commit_shred(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        plan: _RawExpiryPlan,
        destruction_evidence: str,
    ) -> dict[str, Any]:
        claim = context.claim
        generation = _dek_generation(context)
        receipt_id = self.id_generator()
        event_id = self.id_generator()
        subject_pseudonym = hashlib.sha256(
            f"{scope.namespace_id}\x1f{scope.subject_id}\x1fraw".encode()
        ).hexdigest()
        destroyed = [
            {
                "resource_type": "RawIngressRecord",
                "retention_domain": RAW_RETENTION_DOMAIN,
                "dek_generation": generation,
                "object_count": plan.raw_count,
            }
        ]
        receipt = {
            "schema_version": "shred_receipt.v2",
            "job_kind": "scheduled_expiry",
            "retention_domain": RAW_RETENTION_DOMAIN,
            "dek_generation": generation,
            "destroyed": destroyed,
            "expired_binding_count": plan.binding_count,
            "retained_derived_domains": ["episode", "today_projection"],
            "reason_code": "bounded_replay_raw_expired",
            "destruction_evidence_sha256": hashlib.sha256(
                destruction_evidence.encode()
            ).hexdigest(),
        }
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT status, wrapped_dek "
                    "FROM public.backend_retention_deks "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND subject_id = %s AND retention_domain = 'raw' "
                    "AND generation = %s FOR UPDATE",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                    ),
                )
                dek_row = cursor.fetchone()
                cursor.execute(
                    "SELECT count(retention_binding_id), "
                    "COALESCE(bool_and(expires_at <= clock_timestamp()), FALSE), "
                    "COALESCE(bool_and(legal_hold = FALSE), FALSE) "
                    "FROM public.backend_retention_bindings "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND subject_id = %s AND retention_domain = 'raw' "
                    "AND dek_generation = %s",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                    ),
                )
                binding_row = cursor.fetchone()
                if (
                    dek_row is None
                    or str(dek_row[0]) != "retired"
                    or dek_row[1] is None
                    or bytes(dek_row[1]) != plan.wrapped_dek
                    or binding_row is None
                    or int(binding_row[0]) != plan.binding_count
                    or binding_row[1] is not True
                    or binding_row[2] is not True
                ):
                    raise RetentionLeaseLost(
                        "raw retention facts changed before shred"
                    )
                cursor.execute(
                    "SELECT count(*), COALESCE(bool_and(work.status = ANY(%s)), "
                    "FALSE) FROM public.backend_retention_bindings AS binding "
                    "JOIN public.sleep_domain_normalization_work AS work "
                    "ON work.namespace_id = binding.namespace_id "
                    "AND work.data_mode = binding.data_mode "
                    "AND work.raw_ingress_record_id = binding.resource_id "
                    "WHERE binding.namespace_id = %s "
                    "AND binding.data_mode = %s AND binding.subject_id = %s "
                    "AND binding.retention_domain = 'raw' "
                    "AND binding.dek_generation = %s",
                    (
                        sorted(NORMALIZATION_TERMINAL_STATES),
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                    ),
                )
                normalized = cursor.fetchone()
                if (
                    normalized is None
                    or int(normalized[0]) != plan.raw_count
                    or normalized[1] is not True
                ):
                    raise RetentionLeaseLost(
                        "normalization changed before raw shred"
                    )
                cursor.execute(
                    "INSERT INTO public.backend_shred_receipts ("
                    "shred_receipt_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "subject_pseudonym, retention_job_id, retention_domain, "
                    "dek_generation, lease_generation, fencing_token, "
                    "policy_sha256, destroyed_json, expired_json, "
                    "retained_with_reason_json, receipt_json, completed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'raw', "
                    "%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, "
                    "%s::jsonb, clock_timestamp())",
                    (
                        receipt_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        subject_pseudonym,
                        claim.work_id,
                        generation,
                        claim.lease_generation,
                        claim.fencing_token,
                        plan.configuration_sha256,
                        _json(destroyed),
                        _json([{"binding_count": plan.binding_count}]),
                        _json(
                            [
                                {
                                    "retention_domain": "episode",
                                    "reason_code": "derived_product_fact",
                                },
                                {
                                    "retention_domain": "today_projection",
                                    "reason_code": "derived_product_view",
                                },
                            ]
                        ),
                        _json(receipt),
                    ),
                )
                cursor.execute(
                    "UPDATE public.backend_retention_deks "
                    "SET wrapped_dek = NULL, status = 'shredded', "
                    "shredded_at = clock_timestamp() "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND subject_id = %s AND retention_domain = 'raw' "
                    "AND generation = %s AND status = 'retired' "
                    "AND wrapped_dek = %s",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        generation,
                        plan.wrapped_dek,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RetentionLeaseLost("raw DEK shred CAS was lost")
                cursor.execute(
                    "INSERT INTO public.backend_retention_events ("
                    "retention_event_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "retention_job_id, retention_domain, dek_generation, "
                    "sequence, event_type, lease_generation, fencing_token, "
                    "event_json, occurred_at) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, 'raw', %s, 1, "
                    "'crypto_shred_completed', %s, %s, %s::jsonb, "
                    "clock_timestamp())",
                    (
                        event_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        claim.work_id,
                        generation,
                        claim.lease_generation,
                        claim.fencing_token,
                        _json(receipt),
                    ),
                )
                cursor.execute(
                    "SELECT public.sleepagent_finalize_retention_job("
                    "%s, %s, %s, 'succeeded', NULL)",
                    (
                        claim.work_id,
                        claim.lease_generation,
                        claim.fencing_token,
                    ),
                )
                finalized = cursor.fetchone()
                if finalized is None or finalized[0] is not True:
                    raise RetentionLeaseLost(
                        "retention terminal fence was lost"
                    )
            finally:
                cursor.close()
            uow.commit()
        return {
            "schema_version": "raw_retention_result.v1",
            "retention_domain": RAW_RETENTION_DOMAIN,
            "dek_generation": generation,
            "shred_receipt_id": receipt_id,
            "destroyed_object_count": plan.raw_count,
            "derived_data_retained": True,
        }


class SubjectForgetRetentionHandler:
    """Destroy one pre-reset DEK through reset-scoped definer functions."""

    def __init__(
        self,
        *,
        envelope: RetentionKeyEnvelopePort,
        id_generator: UUID7Generator | None = None,
    ) -> None:
        self.envelope = envelope
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _subject_forget_scope(context)
            with worker_uow_factory(context).begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT * FROM public.sleepagent_prepare_demo_reset_key("
                        "%s, %s, %s, %s)",
                        (
                            context.claim.work_id,
                            context.claim.lease_generation,
                            context.claim.fencing_token,
                            self.id_generator(),
                        ),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if row is None:
                    raise RetentionLeaseLost(
                        "reset key preparation lost its fence"
                    )
                uow.commit()
            if (
                str(row[4]) != self.envelope.key_id
                or str(row[5]) != self.envelope.wrapping_algorithm
                or row[6] is None
            ):
                raise RetentionWorkerError("reset key envelope drifted")
            wrapped = bytes(row[6])
            evidence = self.envelope.destroy_wrapped_key(
                wrapped,
                aad=_dek_aad(scope, int(row[3])),
            )
            evidence_sha256 = hashlib.sha256(evidence.encode()).hexdigest()
            key_receipt_id = self.id_generator()
            with worker_uow_factory(context).begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT public.sleepagent_commit_demo_reset_key("
                        "%s, %s, %s, %s, %s, %s)",
                        (
                            context.claim.work_id,
                            context.claim.lease_generation,
                            context.claim.fencing_token,
                            key_receipt_id,
                            self.id_generator(),
                            evidence_sha256,
                        ),
                    )
                    committed = cursor.fetchone()
                finally:
                    cursor.close()
                if committed is None or committed[0] is not True:
                    raise RetentionLeaseLost(
                        "reset key completion lost its fence"
                    )
                uow.commit()
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result={
                    "schema_version": "subject_forget_key_result.v1",
                    "reset_id": str(row[0]),
                    "source_namespace_generation": int(row[1]),
                    "retention_domain": str(row[2]),
                    "dek_generation": int(row[3]),
                    "object_count": int(row[8]),
                    "key_receipt_id": key_receipt_id,
                },
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except RetentionLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="subject_forget_lease_lost_reconciliation_required",
            )
        except (B3ClaimInvariantError, RetentionWorkerError, TypeError, ValueError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="subject_forget_invariant_violation",
            )


class RetentionWorkRouter:
    def __init__(self, *, envelope: RetentionKeyEnvelopePort) -> None:
        self.raw_expiry = RawRetentionWorkHandler(envelope=envelope)
        self.subject_forget = SubjectForgetRetentionHandler(envelope=envelope)

    def __call__(self, context: WorkContext) -> WorkResult:
        job_kind = context.claim.metadata.get("job_kind")
        if job_kind == "scheduled_expiry":
            return self.raw_expiry(context)
        if job_kind == "subject_forget":
            return self.subject_forget(context)
        return WorkResult(
            disposition=WorkDisposition.TERMINAL,
            error_code="retention_job_kind_unsupported",
        )


class DemoResetWorkHandler:
    """Complete the reset root only after all subject-forget key jobs."""

    def __init__(self, *, id_generator: UUID7Generator | None = None) -> None:
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _demo_reset_scope(context)
            claim = context.claim
            with worker_uow_factory(context).begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT reset.required_dek_count, "
                        "count(job.retention_job_id), "
                        "count(*) FILTER (WHERE job.status = 'succeeded'), "
                        "count(*) FILTER (WHERE job.status IN ("
                        "'failed', 'dead_letter', 'reconciliation_required')) "
                        "FROM public.backend_demo_resets_v2 AS reset "
                        "LEFT JOIN public.backend_retention_jobs AS job "
                        "ON job.job_json ->> 'reset_id' = reset.reset_id "
                        "WHERE reset.operation_id = %s "
                        "AND reset.status = 'pending' "
                        "GROUP BY reset.reset_id, reset.required_dek_count",
                        (claim.work_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RetentionLeaseLost(
                            "demo reset draft is unavailable"
                        )
                    required, total, succeeded, failed = map(int, row)
                    if failed:
                        return WorkResult(
                            disposition=WorkDisposition.TERMINAL,
                            error_code="subject_forget_key_failed",
                        )
                    if total != required or succeeded != required:
                        cursor.execute(
                            "SELECT public.sleepagent_wait_demo_reset("
                            "%s, %s, %s, %s, "
                            "clock_timestamp() + interval '1 second')",
                            (
                                claim.work_id,
                                claim.operation_version,
                                claim.lease_generation,
                                claim.fencing_token,
                            ),
                        )
                        waited = cursor.fetchone()
                        if waited is None or waited[0] is not True:
                            raise RetentionLeaseLost(
                                "demo reset wait lost its fence"
                            )
                        uow.commit()
                        return WorkResult(
                            disposition=WorkDisposition.RETRYABLE,
                            error_code="subject_forget_in_progress",
                            retry_after_seconds=1,
                            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
                        )
                    reset_receipt_id = self.id_generator()
                    cursor.execute(
                        "SELECT public.sleepagent_complete_demo_reset("
                        "%s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.operation_version,
                            claim.lease_generation,
                            claim.fencing_token,
                            reset_receipt_id,
                        ),
                    )
                    receipt_row = cursor.fetchone()
                    if receipt_row is None:
                        raise RetentionLeaseLost(
                            "demo reset completion lost its fence"
                        )
                finally:
                    cursor.close()
                uow.commit()
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result={
                    "schema_version": "demo_reset_result.v1",
                    "reset_receipt_id": reset_receipt_id,
                    "target_generation": scope.namespace_generation,
                    "receipt": _mapping(receipt_row[0]),
                },
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except RetentionLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="demo_reset_lease_lost_reconciliation_required",
            )
        except (B3ClaimInvariantError, RetentionWorkerError, TypeError, ValueError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="demo_reset_invariant_violation",
            )


def build_retention_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    selected = set(settings.worker_queues).intersection(
        {RETENTION_QUEUE, DEMO_RESET_QUEUE}
    )
    if not selected:
        return {}
    if settings.process_role != ProcessRole.WORKER:
        raise RetentionWorkerError("retention retention requires a worker profile")
    if settings.data_mode != DataMode.REPLAY:
        raise RetentionWorkerError("bounded raw retention is replay-only")
    key = BackendKeyProvider(settings.deployment_mode).encryption_key(
        settings.encryption_key_ref
    )
    envelope = LocalTestRetentionKeyEnvelope(
        key,
        key_id=settings.encryption_key_ref,
    )
    handlers: dict[str, WorkHandler] = {}
    if RETENTION_QUEUE in settings.worker_queues:
        handlers[RETENTION_QUEUE] = RetentionWorkRouter(envelope=envelope)
    if DEMO_RESET_QUEUE in settings.worker_queues:
        handlers[DEMO_RESET_QUEUE] = DemoResetWorkHandler()
    return handlers


def _retention_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != RETENTION_QUEUE
        or claim.operation_id is not None
        or claim.metadata.get("work_kind") != RETENTION_QUEUE
        or claim.metadata.get("retention_domain") != RAW_RETENTION_DOMAIN
        or claim.metadata.get("job_kind") != "scheduled_expiry"
        or claim.payload.get("schema_version") != "retention_job.v1"
        or claim.payload.get("job_kind") != "scheduled_expiry"
        or claim.payload.get("retention_domain") != RAW_RETENTION_DOMAIN
        or claim.payload.get("dek_generation")
        != claim.metadata.get("dek_generation")
        or claim.payload.get("policy_version") != RAW_EXPIRY_POLICY_VERSION
        or claim.payload.get("authorization_snapshot")
        != claim.authorization_snapshot
    ):
        raise RetentionWorkerError("invalid raw retention claim")
    scope = exact_worker_scope(context, allowed_handler=RETENTION_QUEUE)
    if scope.data_mode != "replay":
        raise RetentionWorkerError("raw retention handler is replay-only")
    return scope


def _subject_forget_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != RETENTION_QUEUE
        or claim.operation_id is not None
        or claim.metadata.get("work_kind") != RETENTION_QUEUE
        or claim.metadata.get("job_kind") != "subject_forget"
        or claim.payload.get("schema_version") != "retention_job.v1"
        or claim.payload.get("job_kind") != "subject_forget"
        or claim.payload.get("retention_domain")
        != claim.metadata.get("retention_domain")
        or claim.payload.get("dek_generation")
        != claim.metadata.get("dek_generation")
        or claim.payload.get("policy_version") != "bounded-replay-forget.v1"
        or not isinstance(claim.payload.get("reset_id"), str)
        or claim.payload.get("authorization_snapshot")
        != claim.authorization_snapshot
    ):
        raise RetentionWorkerError("invalid subject-forget claim")
    return exact_worker_scope(context, allowed_handler=RETENTION_QUEUE)


def _demo_reset_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    payload = claim.payload
    if (
        claim.queue != DEMO_RESET_QUEUE
        or claim.operation_id != claim.work_id
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("operation_type") != DEMO_RESET_QUEUE
        or claim.metadata.get("queue_name") != DEMO_RESET_QUEUE
        or payload.get("schema_version") != "backend_operation.v2"
        or payload.get("command_type") != "demo.reset.v1"
        or payload.get("authorization_snapshot") != claim.authorization_snapshot
    ):
        raise RetentionWorkerError("invalid demo reset claim")
    return exact_worker_scope(context, allowed_handler=DEMO_RESET_QUEUE)


def _dek_generation(context: WorkContext) -> int:
    value = context.claim.metadata.get("dek_generation")
    if type(value) is not int or value < 1:
        raise RetentionWorkerError("invalid raw DEK generation")
    return value


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RetentionWorkerError("reset receipt must be an object")
    return dict(value)


__all__ = [
    "RAW_EXPIRY_POLICY_VERSION",
    "DEMO_RESET_QUEUE",
    "RETENTION_QUEUE",
    "DemoResetWorkHandler",
    "RawRetentionWorkHandler",
    "RetentionWorkRouter",
    "RetentionWorkerError",
    "SubjectForgetRetentionHandler",
    "build_retention_worker_handlers",
]
