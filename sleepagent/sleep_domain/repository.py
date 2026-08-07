"""Unified persistence primitives over the existing RadarPersistenceStore.

The repository deliberately implements storage mechanics only: idempotency,
transactions, leases, CAS, append-only revisions, encryption, and namespace
isolation.  It does not resolve bindings, aggregate NightEpisodes, normalize
provider payloads, invoke Agents, or expose an API.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.sleep_domain.contracts import (
    AdapterDeploymentEvent,
    AdapterDescriptor,
    AdapterObservationCandidate,
    AdapterResolutionLock,
    AlertCorrelationReceipt,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisRevision,
    CapabilityVerificationReceipt,
    DataMode,
    DeterministicQualityAssessment,
    CurrentRisk,
    CareFollowupSnapshot,
    CareFollowupTransitionReceipt,
    DeviceBinding,
    DeviceBindingAuditAction,
    DeviceBindingAuditEvent,
    DeviceBindingStatus,
    DomainEvent,
    FastPathSignalProjection,
    FastPathSignalReceipt,
    LifecycleTransitionReceipt,
    MonitoringSnapshot,
    NightEpisode,
    NightEpisodeRevision,
    Operation,
    OperationStatus,
    ProcessingOutcome,
    ProcessingReceipt,
    ProviderDeviceIdentity,
    PendingEpisodeAssociation,
    QuarantineReprocessAudit,
    RawIngressRecord,
    SleepObservation,
    VendorAlertInstance,
)
from sleepagent.sleep_domain.crypto import RawPayloadEncryptionPolicy


JsonScalar = str | int | float | bool | None


class SleepDomainPersistenceError(RuntimeError):
    pass


class NamespaceMismatchError(SleepDomainPersistenceError):
    pass


class ImmutableRecordConflictError(SleepDomainPersistenceError):
    pass


class IdempotencyConflictError(SleepDomainPersistenceError):
    pass


class ReplayConflictError(SleepDomainPersistenceError):
    pass


class DeviceBindingOverlapError(SleepDomainPersistenceError):
    pass


class DeviceIdentityConflictError(SleepDomainPersistenceError):
    pass


class LeaseConflictError(SleepDomainPersistenceError):
    pass


class CasConflictError(SleepDomainPersistenceError):
    pass


@dataclass(frozen=True)
class DomainNamespace:
    namespace_id: str
    data_mode: DataMode

    def __post_init__(self) -> None:
        expected_prefix = f"{self.data_mode.value}:"
        if not self.namespace_id.startswith(expected_prefix):
            raise NamespaceMismatchError(
                f"{self.data_mode.value} namespace must start with "
                f"{expected_prefix!r}"
            )
        if self.namespace_id == expected_prefix:
            raise NamespaceMismatchError("namespace requires an opaque suffix")


@dataclass(frozen=True)
class ProviderAccountRecord:
    namespace: DomainNamespace
    provider_account_id: str
    provider_id: str
    configuration_fingerprint: str
    status: str
    metadata: Mapping[str, JsonScalar]
    created_at: datetime


@dataclass(frozen=True)
class IntakeResult:
    raw_ingress_record_id: str
    work_id: str
    processing_intent_id: str
    created: bool


@dataclass(frozen=True)
class IngressReplayGuard:
    provider_id: str
    provider_account_id: str
    compatibility_profile_id: str
    nonce: str
    idempotency_identity: str
    pre_normalization_payload_sha256: str
    request_signed_at: datetime


@dataclass(frozen=True)
class TerminalRawQuarantine:
    quarantine_id: str
    receipt: ProcessingReceipt
    detail: Mapping[str, JsonScalar]


@dataclass(frozen=True)
class NormalizationCommitResult:
    observation_id: str
    event: DomainEvent
    created: bool


@dataclass(frozen=True)
class CandidatePromotionCommitResult:
    observation_id: str
    created: bool


@dataclass(frozen=True)
class SourceReportVersion:
    source_report_version_id: str
    provider_id: str
    provider_account_id: str
    provider_device_key: str
    local_report_date: date
    report_version: int
    content_sha256: str
    raw_ingress_record_id: str
    is_empty: bool
    fetched_at: datetime
    created: bool


@dataclass(frozen=True)
class PullCheckpoint:
    checkpoint_id: str
    provider_id: str
    provider_account_id: str
    stream_key: str
    cursor_at: datetime
    lateness_watermark_at: datetime
    cas_version: int
    updated_at: datetime


@dataclass(frozen=True)
class QuarantineEntry:
    quarantine_id: str
    raw_ingress_record_id: str
    reason: str
    detail_code: str | None
    receipt_id: str
    detail: Mapping[str, JsonScalar]
    quarantined_at: datetime


@dataclass(frozen=True)
class NormalizationWorkLease:
    work_id: str
    raw_ingress_record_id: str
    work_generation: int
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime
    work_json: str


@dataclass(frozen=True)
class OutboxLease:
    outbox_kind: str
    item_id: str
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime
    payload_json: str


@dataclass(frozen=True)
class CurrentRevisionPointer:
    night_episode_id: str
    current_revision_id: str | None
    current_revision_number: int | None
    cas_version: int


@dataclass(frozen=True)
class SubjectLifecycleLease:
    subject_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class EpisodeObservationMembership:
    membership_id: str
    night_episode_id: str
    observation_id: str
    subject_id: str
    device_binding_id: str
    binding_version: int
    event_at: datetime
    received_at: datetime
    lateness_watermark_at: datetime | None
    late_after_watermark: bool
    associated_at: datetime


@dataclass(frozen=True)
class NightRevisionCommitResult:
    revision: NightEpisodeRevision
    episode: NightEpisode
    event: DomainEvent
    created: bool


@dataclass(frozen=True)
class ObservationConflictRecord:
    conflict_id: str
    fact_slot_key: str
    first_observation_id: str
    second_observation_id: str
    detected_at: datetime


@dataclass(frozen=True)
class LeasedOperation:
    operation: Operation
    cas_version: int


class SleepDomainRepository:
    """Provider-neutral repository sharing the Radar persistence connection."""

    def __init__(
        self,
        store: RadarPersistenceStore,
        *,
        raw_payload_policy: RawPayloadEncryptionPolicy,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.dialect = store.dialect
        self.raw_payload_policy = raw_payload_policy

    def save_adapter_descriptor(
        self,
        namespace: DomainNamespace,
        descriptor: AdapterDescriptor,
        *,
        created_at: datetime,
    ) -> AdapterDescriptor:
        if namespace.data_mode not in descriptor.supported_data_modes:
            raise NamespaceMismatchError(
                "adapter descriptor does not support namespace data_mode"
            )
        descriptor_json = descriptor.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT descriptor_json
                    FROM sleep_domain_adapters
                    WHERE namespace_id = ? AND data_mode = ?
                      AND adapter_id = ? AND adapter_version = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    descriptor.adapter_id,
                    descriptor.adapter_version,
                ),
            ).fetchone()
            if row is not None:
                existing_json = _database_json_text(row[0])
                if not _json_equivalent(existing_json, descriptor_json):
                    raise ImmutableRecordConflictError(
                        "adapter version is immutable"
                    )
                return AdapterDescriptor.model_validate_json(existing_json)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_adapters (
                      namespace_id, data_mode, adapter_id, adapter_version,
                      provider_id, contract_version, configuration_fingerprint,
                      deployment_status, descriptor_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    descriptor.adapter_id,
                    descriptor.adapter_version,
                    descriptor.provider_id,
                    descriptor.contract_version,
                    descriptor.configuration_fingerprint,
                    descriptor.deployment_status.value,
                    descriptor_json,
                    _dump_datetime(created_at),
                ),
            )
        return descriptor

    def get_adapter_descriptor(
        self,
        namespace: DomainNamespace,
        *,
        adapter_id: str,
        adapter_version: str,
    ) -> AdapterDescriptor | None:
        row = self._fetchone(
            """
            SELECT descriptor_json
            FROM sleep_domain_adapters
            WHERE namespace_id = ? AND data_mode = ?
              AND adapter_id = ? AND adapter_version = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                adapter_id,
                adapter_version,
            ),
        )
        if row is None:
            return None
        return AdapterDescriptor.model_validate_json(_database_json_text(row[0]))

    def append_adapter_deployment_event(
        self,
        namespace: DomainNamespace,
        event: AdapterDeploymentEvent,
    ) -> AdapterDeploymentEvent:
        if event.data_mode != namespace.data_mode:
            raise NamespaceMismatchError(
                "deployment event data_mode does not match namespace"
            )
        event_json = event.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT event_json
                    FROM sleep_domain_adapter_deployment_events
                    WHERE deployment_event_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    event.deployment_event_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if row is not None:
                existing = _database_json_text(row[0])
                if not _json_equivalent(existing, event_json):
                    raise ImmutableRecordConflictError(
                        "adapter deployment event is immutable"
                    )
                return AdapterDeploymentEvent.model_validate_json(existing)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_adapter_deployment_events (
                      deployment_event_id, namespace_id, data_mode,
                      adapter_id, adapter_version, previous_status,
                      deployment_status, event_json, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    event.deployment_event_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    event.adapter_id,
                    event.adapter_version,
                    event.previous_status.value,
                    event.deployment_status.value,
                    event_json,
                    _dump_datetime(event.changed_at),
                ),
            )
        return event

    def latest_adapter_deployment_event(
        self,
        namespace: DomainNamespace,
        *,
        adapter_id: str,
        adapter_version: str,
    ) -> AdapterDeploymentEvent | None:
        row = self._fetchone(
            """
            SELECT event_json
            FROM sleep_domain_adapter_deployment_events
            WHERE namespace_id = ? AND data_mode = ?
              AND adapter_id = ? AND adapter_version = ?
            ORDER BY changed_at DESC, deployment_event_id DESC
            LIMIT 1
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                adapter_id,
                adapter_version,
            ),
        )
        if row is None:
            return None
        return AdapterDeploymentEvent.model_validate_json(
            _database_json_text(row[0])
        )

    def save_capability_verification_receipt(
        self,
        namespace: DomainNamespace,
        receipt: CapabilityVerificationReceipt,
    ) -> CapabilityVerificationReceipt:
        if receipt.data_mode != namespace.data_mode:
            raise NamespaceMismatchError(
                "verification receipt data_mode does not match namespace"
            )
        receipt_json = receipt.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT receipt_json
                    FROM sleep_domain_capability_verification_receipts
                    WHERE receipt_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    receipt.receipt_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if row is not None:
                existing = _database_json_text(row[0])
                if not _json_equivalent(existing, receipt_json):
                    raise ImmutableRecordConflictError(
                        "capability verification receipt is immutable"
                    )
                return CapabilityVerificationReceipt.model_validate_json(existing)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_capability_verification_receipts (
                      receipt_id, namespace_id, data_mode, adapter_id,
                      adapter_version, capability, environment,
                      verification_status, receipt_json, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    receipt.receipt_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    receipt.adapter_id,
                    receipt.adapter_version,
                    receipt.capability.value,
                    receipt.environment,
                    receipt.status.value,
                    receipt_json,
                    _dump_datetime(receipt.reviewed_at),
                ),
            )
        return receipt

    def latest_capability_verification_receipt(
        self,
        namespace: DomainNamespace,
        *,
        adapter_id: str,
        adapter_version: str,
        capability: str,
        environment: str,
    ) -> CapabilityVerificationReceipt | None:
        row = self._fetchone(
            """
            SELECT receipt_json
            FROM sleep_domain_capability_verification_receipts
            WHERE namespace_id = ? AND data_mode = ?
              AND adapter_id = ? AND adapter_version = ?
              AND capability = ? AND environment = ?
            ORDER BY reviewed_at DESC, receipt_id DESC
            LIMIT 1
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                adapter_id,
                adapter_version,
                capability,
                environment,
            ),
        )
        if row is None:
            return None
        return CapabilityVerificationReceipt.model_validate_json(
            _database_json_text(row[0])
        )

    def save_adapter_resolution_lock(
        self,
        namespace: DomainNamespace,
        lock: AdapterResolutionLock,
    ) -> AdapterResolutionLock:
        if lock.data_mode != namespace.data_mode:
            raise NamespaceMismatchError(
                "resolution lock data_mode does not match namespace"
            )
        lock_json = lock.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                self._sql(
                    """
                    SELECT adapter_resolution_lock_id, lock_json
                    FROM sleep_domain_adapter_resolution_locks
                    WHERE namespace_id = ? AND data_mode = ?
                      AND (
                        adapter_resolution_lock_id = ?
                        OR resolution_request_id = ?
                      )
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    lock.adapter_resolution_lock_id,
                    lock.resolution_request_id,
                ),
            ).fetchall()
            if rows:
                for row in rows:
                    existing = _database_json_text(row[1])
                    if (
                        str(row[0]) == lock.adapter_resolution_lock_id
                        and _json_equivalent(existing, lock_json)
                    ):
                        return AdapterResolutionLock.model_validate_json(existing)
                    existing_lock = AdapterResolutionLock.model_validate_json(
                        existing
                    )
                    if (
                        existing_lock.resolution_request_id
                        == lock.resolution_request_id
                        and _json_equivalent(existing, lock_json)
                    ):
                        return existing_lock
                raise ImmutableRecordConflictError(
                    "resolution lock id or request id already has another value"
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_adapter_resolution_locks (
                      adapter_resolution_lock_id, namespace_id, data_mode,
                      provider_id, provider_account_id, adapter_id,
                      adapter_version, environment, resolution_request_id,
                      lock_json, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    lock.adapter_resolution_lock_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    lock.provider_id,
                    lock.provider_account_id,
                    lock.adapter_id,
                    lock.adapter_version,
                    lock.environment,
                    lock.resolution_request_id,
                    lock_json,
                    _dump_datetime(lock.resolved_at),
                ),
            )
        return lock

    def load_adapter_resolution_lock(
        self,
        namespace: DomainNamespace,
        *,
        adapter_resolution_lock_id: str,
    ) -> AdapterResolutionLock | None:
        row = self._fetchone(
            """
            SELECT lock_json
            FROM sleep_domain_adapter_resolution_locks
            WHERE namespace_id = ? AND data_mode = ?
              AND adapter_resolution_lock_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                adapter_resolution_lock_id,
            ),
        )
        if row is None:
            return None
        return AdapterResolutionLock.model_validate_json(
            _database_json_text(row[0])
        )

    def count_adapter_resolution_references(
        self,
        namespace: DomainNamespace,
        *,
        adapter_id: str,
        adapter_version: str,
    ) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*)
            FROM sleep_domain_adapter_resolution_locks
            WHERE namespace_id = ? AND data_mode = ?
              AND adapter_id = ? AND adapter_version = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                adapter_id,
                adapter_version,
            ),
        )
        return int(row[0]) if row is not None else 0

    def save_provider_account(
        self,
        record: ProviderAccountRecord,
    ) -> ProviderAccountRecord:
        _require_non_empty(record.provider_account_id, "provider_account_id")
        _require_non_empty(record.provider_id, "provider_id")
        _require_sha256(
            record.configuration_fingerprint,
            "configuration_fingerprint",
        )
        _reject_secret_metadata(record.metadata)
        metadata_json = _dump_plain_json(dict(record.metadata))
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT provider_id, configuration_fingerprint, status,
                           account_metadata_json, created_at
                    FROM sleep_domain_provider_accounts
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_account_id = ?
                    """
                ),
                (
                    record.namespace.namespace_id,
                    record.namespace.data_mode.value,
                    record.provider_account_id,
                ),
            ).fetchone()
            expected = (
                record.provider_id,
                record.configuration_fingerprint,
                record.status,
                metadata_json,
                _dump_datetime(record.created_at),
            )
            if row is not None:
                actual = (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    _canonical_json_text(row[3]),
                    _database_datetime_text(row[4]),
                )
                if actual != expected:
                    raise ImmutableRecordConflictError(
                        "provider account record is immutable"
                    )
                return record
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_provider_accounts (
                      namespace_id, data_mode, provider_account_id, provider_id,
                      configuration_fingerprint, status,
                      account_metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    record.namespace.namespace_id,
                    record.namespace.data_mode.value,
                    record.provider_account_id,
                    record.provider_id,
                    record.configuration_fingerprint,
                    record.status,
                    metadata_json,
                    _dump_datetime(record.created_at),
                ),
            )
        return record

    def get_provider_account(
        self,
        namespace: DomainNamespace,
        *,
        provider_account_id: str,
    ) -> ProviderAccountRecord | None:
        row = self._fetchone(
            """
            SELECT provider_id, configuration_fingerprint, status,
                   account_metadata_json, created_at
            FROM sleep_domain_provider_accounts
            WHERE namespace_id = ? AND data_mode = ?
              AND provider_account_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                provider_account_id,
            ),
        )
        if row is None:
            return None
        return ProviderAccountRecord(
            namespace=namespace,
            provider_account_id=provider_account_id,
            provider_id=str(row[0]),
            configuration_fingerprint=str(row[1]),
            status=str(row[2]),
            metadata=json.loads(_database_json_text(row[3])),
            created_at=_parse_datetime(str(row[4])),
        )

    def append_device_binding(
        self,
        namespace: DomainNamespace,
        binding: DeviceBinding,
    ) -> DeviceBinding:
        self._require_mode(namespace, binding.data_mode)
        binding_json = binding.model_dump_json()
        effective_from = _dump_datetime(binding.effective_from)
        effective_until = _dump_optional_datetime(binding.effective_until)
        with self._transaction(immediate=True) as cursor:
            self._lock_provider_account(
                cursor,
                namespace,
                provider_account_id=binding.provider_account_id,
                expected_provider_id=binding.provider_id,
            )
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT binding_json
                    FROM sleep_domain_device_bindings
                    WHERE device_binding_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    binding.device_binding_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                existing_json = _database_json_text(existing[0])
                if not _json_equivalent(existing_json, binding_json):
                    raise ImmutableRecordConflictError(
                        "DeviceBinding is append-only"
                    )
                return DeviceBinding.model_validate_json(existing_json)

            overlap_sql = """
                SELECT device_binding_id
                FROM sleep_domain_device_bindings
                WHERE namespace_id = ? AND data_mode = ? AND device_id = ?
                  AND (effective_until IS NULL OR effective_until > ?)
            """
            overlap_params: list[Any] = [
                namespace.namespace_id,
                namespace.data_mode.value,
                binding.device_id,
                effective_from,
            ]
            if effective_until is not None:
                overlap_sql += " AND effective_from < ?"
                overlap_params.append(effective_until)
            overlap_sql += " LIMIT 1"
            overlap = cursor.execute(
                self._sql(overlap_sql),
                tuple(overlap_params),
            ).fetchone()
            if overlap is not None:
                raise DeviceBindingOverlapError(
                    f"binding interval overlaps {overlap[0]}"
                )

            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_device_bindings (
                      device_binding_id, namespace_id, data_mode,
                      binding_version, device_id, provider_id,
                      provider_account_id, subject_id, timezone_name,
                      effective_from, effective_until, status, binding_json,
                      recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    binding.device_binding_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    binding.binding_version,
                    binding.device_id,
                    binding.provider_id,
                    binding.provider_account_id,
                    binding.subject_id,
                    binding.timezone_name,
                    effective_from,
                    effective_until,
                    binding.status.value,
                    binding_json,
                    _dump_datetime(binding.recorded_at),
                ),
            )
        return binding

    def compare_and_set_device_binding(
        self,
        namespace: DomainNamespace,
        *,
        binding: DeviceBinding,
        expected_binding_version: int,
        provider_device_key: str,
        audit_event: DeviceBindingAuditEvent,
    ) -> DeviceBinding:
        """Create or prospectively replace one binding under persistent CAS.

        The previous version is closed only inside the same transaction that
        appends the new version and its audit event. Retrying the same
        ``command_id`` is idempotent; a competing command with the same expected
        version receives a CAS conflict.
        """

        self._require_mode(namespace, binding.data_mode)
        self._require_mode(namespace, audit_event.data_mode)
        if expected_binding_version < 0:
            raise ValueError("expected_binding_version must be non-negative")
        if binding.binding_version != expected_binding_version + 1:
            raise ValueError("new binding_version must equal expected version + 1")
        if (
            audit_event.provider_id != binding.provider_id
            or audit_event.provider_account_id != binding.provider_account_id
            or audit_event.provider_device_key != provider_device_key
            or audit_event.device_id != binding.device_id
            or audit_event.new_device_binding_id != binding.device_binding_id
            or audit_event.new_binding_version != binding.binding_version
            or audit_event.effective_at != binding.effective_from
        ):
            raise ValueError("binding audit event does not match binding command")

        binding_json = binding.model_dump_json()
        audit_json = audit_event.model_dump_json()
        provider_device_json = binding.provider_device.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            self._lock_provider_account(
                cursor,
                namespace,
                provider_account_id=binding.provider_account_id,
                expected_provider_id=binding.provider_id,
            )
            prior_audit = cursor.execute(
                self._sql(
                    """
                    SELECT audit_json
                    FROM sleep_domain_device_binding_audit
                    WHERE namespace_id = ? AND data_mode = ? AND command_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    audit_event.command_id,
                ),
            ).fetchone()
            if prior_audit is not None:
                prior_json = _database_json_text(prior_audit[0])
                if not _json_equivalent(prior_json, audit_json):
                    raise ImmutableRecordConflictError(
                        "binding command id already has different content"
                    )
                stored = cursor.execute(
                    self._sql(
                        """
                        SELECT binding_json
                        FROM sleep_domain_device_bindings
                        WHERE device_binding_id = ? AND namespace_id = ?
                          AND data_mode = ?
                        """
                    ),
                    (
                        binding.device_binding_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                if stored is None:
                    raise SleepDomainPersistenceError(
                        "binding audit exists without its binding"
                    )
                return DeviceBinding.model_validate_json(
                    _database_json_text(stored[0])
                )

            identity = cursor.execute(
                self._sql(
                    """
                    SELECT device_id, provider_device_json
                    FROM sleep_domain_device_identities
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_id = ? AND provider_account_id = ?
                      AND provider_device_key = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    binding.provider_id,
                    binding.provider_account_id,
                    provider_device_key,
                ),
            ).fetchone()
            if identity is None:
                device_alias = cursor.execute(
                    self._sql(
                        """
                        SELECT provider_device_key
                        FROM sleep_domain_device_identities
                        WHERE namespace_id = ? AND data_mode = ? AND device_id = ?
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        binding.device_id,
                    ),
                ).fetchone()
                if device_alias is not None:
                    raise DeviceIdentityConflictError(
                        "internal device_id is already assigned to another "
                        "namespaced provider device"
                    )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_device_identities (
                          namespace_id, data_mode, provider_id,
                          provider_account_id, provider_device_key, device_id,
                          provider_device_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        binding.provider_id,
                        binding.provider_account_id,
                        provider_device_key,
                        binding.device_id,
                        provider_device_json,
                        _dump_datetime(audit_event.occurred_at),
                    ),
                )
            elif (
                str(identity[0]) != binding.device_id
                or not _json_equivalent(identity[1], provider_device_json)
            ):
                raise DeviceIdentityConflictError(
                    "namespaced provider device is already assigned differently"
                )

            rows = cursor.execute(
                self._sql(
                    """
                    SELECT binding_json
                    FROM sleep_domain_device_bindings
                    WHERE namespace_id = ? AND data_mode = ? AND device_id = ?
                    ORDER BY binding_version
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    binding.device_id,
                ),
            ).fetchall()
            history = [
                DeviceBinding.model_validate_json(_database_json_text(row[0]))
                for row in rows
            ]
            current = history[-1] if history else None
            current_version = 0 if current is None else current.binding_version
            if current_version != expected_binding_version:
                raise CasConflictError(
                    "device binding version changed before command commit"
                )

            if current is None:
                if audit_event.action != DeviceBindingAuditAction.CREATED:
                    raise ValueError("initial binding requires a created audit action")
            else:
                if audit_event.action != DeviceBindingAuditAction.REBOUND:
                    raise ValueError("binding replacement requires rebound action")
                if (
                    audit_event.previous_device_binding_id
                    != current.device_binding_id
                    or audit_event.previous_binding_version
                    != current.binding_version
                ):
                    raise ValueError(
                        "binding audit references a stale previous binding"
                    )
                if binding.effective_from < audit_event.occurred_at:
                    raise ValueError("rebinding must be prospective")
                if binding.effective_from <= current.effective_from:
                    raise DeviceBindingOverlapError(
                        "rebinding effective time must follow the previous start"
                    )
                if (
                    current.effective_until is not None
                    and binding.effective_from < current.effective_until
                ):
                    raise DeviceBindingOverlapError(
                        "rebinding overlaps the previous explicit interval"
                    )
                if current.effective_until is None:
                    closed = current.model_copy(
                        update={
                            "effective_until": binding.effective_from,
                            "status": DeviceBindingStatus.ENDED,
                        }
                    )
                    cursor.execute(
                        self._sql(
                            """
                            UPDATE sleep_domain_device_bindings
                            SET effective_until = ?, status = ?, binding_json = ?
                            WHERE device_binding_id = ? AND namespace_id = ?
                              AND data_mode = ? AND effective_until IS NULL
                            """
                        ),
                        (
                            _dump_datetime(binding.effective_from),
                            DeviceBindingStatus.ENDED.value,
                            closed.model_dump_json(),
                            current.device_binding_id,
                            namespace.namespace_id,
                            namespace.data_mode.value,
                        ),
                    )
                    history[-1] = closed

            for historical in history:
                if (
                    historical.effective_until is None
                    or historical.effective_until > binding.effective_from
                ) and (
                    binding.effective_until is None
                    or historical.effective_from < binding.effective_until
                ):
                    raise DeviceBindingOverlapError(
                        f"binding interval overlaps {historical.device_binding_id}"
                    )

            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_device_bindings (
                      device_binding_id, namespace_id, data_mode,
                      binding_version, device_id, provider_id,
                      provider_account_id, subject_id, timezone_name,
                      effective_from, effective_until, status, binding_json,
                      recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    binding.device_binding_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    binding.binding_version,
                    binding.device_id,
                    binding.provider_id,
                    binding.provider_account_id,
                    binding.subject_id,
                    binding.timezone_name,
                    _dump_datetime(binding.effective_from),
                    _dump_optional_datetime(binding.effective_until),
                    binding.status.value,
                    binding_json,
                    _dump_datetime(binding.recorded_at),
                ),
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_device_binding_audit (
                      audit_event_id, namespace_id, data_mode, command_id,
                      action, provider_id, provider_account_id,
                      provider_device_key, device_id,
                      previous_binding_version, new_binding_version,
                      actor_id, authorization_id, audit_json, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    audit_event.audit_event_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    audit_event.command_id,
                    audit_event.action.value,
                    audit_event.provider_id,
                    audit_event.provider_account_id,
                    audit_event.provider_device_key,
                    audit_event.device_id,
                    audit_event.previous_binding_version,
                    audit_event.new_binding_version,
                    audit_event.actor_id,
                    audit_event.authorization_id,
                    audit_json,
                    _dump_datetime(audit_event.occurred_at),
                ),
            )
        return binding

    def matching_device_bindings(
        self,
        namespace: DomainNamespace,
        *,
        provider_id: str,
        provider_account_id: str,
        provider_device_key: str,
    ) -> tuple[DeviceBinding, ...]:
        """Return exact namespaced identity matches, without choosing a current row."""

        identity = self._fetchone(
            """
            SELECT device_id
            FROM sleep_domain_device_identities
            WHERE namespace_id = ? AND data_mode = ? AND provider_id = ?
              AND provider_account_id = ? AND provider_device_key = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                provider_id,
                provider_account_id,
                provider_device_key,
            ),
        )
        if identity is None:
            return ()
        rows = self._fetchall(
            """
            SELECT binding_json
            FROM sleep_domain_device_bindings
            WHERE namespace_id = ? AND data_mode = ? AND device_id = ?
            ORDER BY effective_from, binding_version
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                str(identity[0]),
            ),
        )
        return tuple(
            DeviceBinding.model_validate_json(
                _database_json_text(row[0])
            )
            for row in rows
        )

    def list_device_bindings(
        self,
        namespace: DomainNamespace,
        *,
        device_id: str,
    ) -> tuple[DeviceBinding, ...]:
        rows = self._fetchall(
            """
            SELECT binding_json
            FROM sleep_domain_device_bindings
            WHERE namespace_id = ? AND data_mode = ? AND device_id = ?
            ORDER BY binding_version, effective_from
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                device_id,
            ),
        )
        return tuple(
            DeviceBinding.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def list_subject_device_bindings(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
    ) -> tuple[DeviceBinding, ...]:
        rows = self._fetchall(
            """
            SELECT binding_json
            FROM sleep_domain_device_bindings
            WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
            ORDER BY effective_from, binding_version, device_binding_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                subject_id,
            ),
        )
        return tuple(
            DeviceBinding.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def get_device_binding(
        self,
        namespace: DomainNamespace,
        *,
        device_binding_id: str,
    ) -> DeviceBinding | None:
        row = self._fetchone(
            """
            SELECT binding_json
            FROM sleep_domain_device_bindings
            WHERE device_binding_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                device_binding_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return DeviceBinding.model_validate_json(_database_json_text(row[0]))

    def intake_raw(
        self,
        namespace: DomainNamespace,
        record: RawIngressRecord,
        *,
        raw_payload: bytes,
        work_id: str,
        work_generation: int,
        work_json: Mapping[str, JsonScalar],
        processing_intent_id: str,
        processing_intent_json: Mapping[str, JsonScalar],
        created_at: datetime,
        replay_guard: IngressReplayGuard | None = None,
        processing_intent_event_type: str = "RAW_ACCEPTED",
        terminal_quarantine: TerminalRawQuarantine | None = None,
    ) -> IntakeResult:
        """Atomically write raw + normalization work + processing intent only."""

        self._require_mode(namespace, record.data_mode)
        if len(raw_payload) != record.payload_size_bytes:
            raise ValueError("raw payload size does not match metadata")
        payload_hash = hashlib.sha256(raw_payload).hexdigest()
        if payload_hash != record.pre_normalization_payload_sha256:
            raise ValueError("raw payload hash does not match metadata")
        self.raw_payload_policy.validate_retention(
            received_at=record.received_at,
            retention_until=record.retention_deadline,
        )
        encrypted = self.raw_payload_policy.encrypt(
            raw_ingress_record_id=record.raw_ingress_record_id,
            namespace_id=namespace.namespace_id,
            payload=raw_payload,
            encrypted_at=created_at,
        )
        work_json_text = _dump_plain_json(dict(work_json))
        intent_json_text = _dump_plain_json(dict(processing_intent_json))
        _require_non_empty(
            processing_intent_event_type,
            "processing_intent_event_type",
        )
        if terminal_quarantine is not None:
            quarantine_receipt = terminal_quarantine.receipt
            self._require_mode(namespace, quarantine_receipt.data_mode)
            if (
                quarantine_receipt.raw_ingress_record_id
                != record.raw_ingress_record_id
                or quarantine_receipt.outcome
                != ProcessingOutcome.QUARANTINED
                or quarantine_receipt.quarantine_reason is None
            ):
                raise ValueError(
                    "terminal quarantine must match raw and carry a typed reason"
                )

        with self._transaction(immediate=True) as cursor:
            self._lock_provider_account(
                cursor,
                namespace,
                provider_account_id=record.provider_account_id,
                expected_provider_id=record.provider_id,
            )
            nonce_already_recorded = False
            if replay_guard is not None:
                if (
                    replay_guard.provider_id != record.provider_id
                    or replay_guard.provider_account_id
                    != record.provider_account_id
                    or replay_guard.idempotency_identity
                    != record.idempotency_identity
                    or replay_guard.pre_normalization_payload_sha256
                    != payload_hash
                    or record.request_signed_at != replay_guard.request_signed_at
                ):
                    raise ValueError("replay guard does not match raw record")
            idempotent = cursor.execute(
                self._sql(
                    """
                    SELECT raw_ingress_record_id,
                           pre_normalization_payload_sha256
                    FROM sleep_domain_raw_inbox
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_account_id = ?
                      AND idempotency_identity = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    record.provider_account_id,
                    record.idempotency_identity,
                ),
            ).fetchone()
            if idempotent is not None:
                if str(idempotent[1]) != payload_hash:
                    raise IdempotencyConflictError(
                        "idempotency identity has a different raw payload hash"
                    )
                if replay_guard is not None:
                    nonce_already_recorded = (
                        self._ingress_nonce_already_recorded(
                            cursor,
                            namespace,
                            replay_guard=replay_guard,
                        )
                    )
                existing_raw_id = str(idempotent[0])
                existing_work = cursor.execute(
                    self._sql(
                        """
                        SELECT work_id
                        FROM sleep_domain_normalization_work
                        WHERE namespace_id = ? AND data_mode = ?
                          AND raw_ingress_record_id = ?
                          AND work_generation = ?
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        existing_raw_id,
                        work_generation,
                    ),
                ).fetchone()
                existing_intent = cursor.execute(
                    self._sql(
                        """
                        SELECT intent_id
                        FROM sleep_domain_processing_outbox
                        WHERE namespace_id = ? AND data_mode = ?
                          AND raw_ingress_record_id = ?
                        ORDER BY created_at, intent_id
                        LIMIT 1
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        existing_raw_id,
                    ),
                ).fetchone()
                if existing_work is None or existing_intent is None:
                    raise SleepDomainPersistenceError(
                        "partial intake detected for an idempotent raw record"
                    )
                if replay_guard is not None and not nonce_already_recorded:
                    self._insert_ingress_nonce(
                        cursor,
                        namespace,
                        replay_guard=replay_guard,
                        raw_ingress_record_id=existing_raw_id,
                        recorded_at=created_at,
                    )
                if terminal_quarantine is not None:
                    self._terminally_quarantine_work(
                        cursor,
                        namespace,
                        work_id=str(existing_work[0]),
                        terminal=terminal_quarantine,
                        completed_at=created_at,
                    )
                return IntakeResult(
                    raw_ingress_record_id=existing_raw_id,
                    work_id=str(existing_work[0]),
                    processing_intent_id=str(existing_intent[0]),
                    created=False,
                )

            if replay_guard is not None:
                nonce_already_recorded = self._ingress_nonce_already_recorded(
                    cursor,
                    namespace,
                    replay_guard=replay_guard,
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_raw_inbox (
                      raw_ingress_record_id, namespace_id, data_mode,
                      provider_id, provider_account_id, event_type, message_id,
                      request_signed_at, measurement_at, event_occurred_at,
                      received_at, signature_profile, signature_verification,
                      idempotency_identity, idempotency_version,
                      pre_normalization_payload_sha256, encrypted_payload,
                      encryption_key_id, encrypted_at, content_type,
                      payload_size_bytes, retention_until, raw_metadata_json
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?
                    )
                    """
                ),
                (
                    record.raw_ingress_record_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    record.provider_id,
                    record.provider_account_id,
                    record.event_type,
                    record.message_id,
                    _dump_optional_datetime(record.request_signed_at),
                    _dump_optional_datetime(record.measurement_at),
                    _dump_optional_datetime(record.event_occurred_at),
                    _dump_datetime(record.received_at),
                    record.signature_profile,
                    record.signature_verification.value,
                    record.idempotency_identity,
                    record.idempotency_version,
                    payload_hash,
                    encrypted.ciphertext,
                    encrypted.key_id,
                    _dump_datetime(encrypted.encrypted_at),
                    record.content_type,
                    record.payload_size_bytes,
                    _dump_datetime(record.retention_deadline),
                    record.model_dump_json(),
                ),
            )
            if replay_guard is not None and not nonce_already_recorded:
                self._insert_ingress_nonce(
                    cursor,
                    namespace,
                    replay_guard=replay_guard,
                    raw_ingress_record_id=record.raw_ingress_record_id,
                    recorded_at=created_at,
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_normalization_work (
                      work_id, namespace_id, data_mode, raw_ingress_record_id,
                      work_generation, status, attempt_count, available_at,
                      lease_owner, lease_expires_at, last_error_code, work_json,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    record.raw_ingress_record_id,
                    work_generation,
                    "pending",
                    0,
                    _dump_datetime(created_at),
                    None,
                    None,
                    None,
                    work_json_text,
                    _dump_datetime(created_at),
                    _dump_datetime(created_at),
                ),
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_processing_outbox (
                      intent_id, namespace_id, data_mode,
                      raw_ingress_record_id, event_type, status,
                      attempt_count, available_at, lease_owner,
                      lease_expires_at, intent_json, created_at, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    processing_intent_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    record.raw_ingress_record_id,
                    processing_intent_event_type,
                    "pending",
                    0,
                    _dump_datetime(created_at),
                    None,
                    None,
                    intent_json_text,
                    _dump_datetime(created_at),
                    None,
                ),
            )
            if terminal_quarantine is not None:
                self._terminally_quarantine_work(
                    cursor,
                    namespace,
                    work_id=work_id,
                    terminal=terminal_quarantine,
                    completed_at=created_at,
                )
        return IntakeResult(
            raw_ingress_record_id=record.raw_ingress_record_id,
            work_id=work_id,
            processing_intent_id=processing_intent_id,
            created=True,
        )

    def register_source_report_version(
        self,
        namespace: DomainNamespace,
        *,
        source_report_version_id: str,
        provider_id: str,
        provider_account_id: str,
        provider_device_key: str,
        local_report_date: date,
        content_sha256: str,
        raw_ingress_record_id: str,
        is_empty: bool,
        fetched_at: datetime,
    ) -> SourceReportVersion:
        """Append one content-addressed vendor report version."""

        with self._transaction(immediate=True) as cursor:
            self._lock_provider_account(
                cursor,
                namespace,
                provider_account_id=provider_account_id,
                expected_provider_id=provider_id,
            )
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT source_report_version_id, report_version,
                           raw_ingress_record_id, is_empty, fetched_at
                    FROM sleep_domain_source_reports
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_id = ? AND provider_account_id = ?
                      AND provider_device_key = ? AND local_report_date = ?
                      AND content_sha256 = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    provider_id,
                    provider_account_id,
                    provider_device_key,
                    local_report_date.isoformat(),
                    content_sha256,
                ),
            ).fetchone()
            if existing is not None:
                return SourceReportVersion(
                    source_report_version_id=str(existing[0]),
                    provider_id=provider_id,
                    provider_account_id=provider_account_id,
                    provider_device_key=provider_device_key,
                    local_report_date=local_report_date,
                    report_version=int(existing[1]),
                    content_sha256=content_sha256,
                    raw_ingress_record_id=str(existing[2]),
                    is_empty=bool(existing[3]),
                    fetched_at=_parse_datetime(str(existing[4])),
                    created=False,
                )
            row = cursor.execute(
                self._sql(
                    """
                    SELECT COALESCE(MAX(report_version), 0)
                    FROM sleep_domain_source_reports
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_id = ? AND provider_account_id = ?
                      AND provider_device_key = ? AND local_report_date = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    provider_id,
                    provider_account_id,
                    provider_device_key,
                    local_report_date.isoformat(),
                ),
            ).fetchone()
            report_version = int(row[0]) + 1
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_source_reports (
                      source_report_version_id, namespace_id, data_mode,
                      provider_id, provider_account_id, provider_device_key,
                      local_report_date, report_version, content_sha256,
                      raw_ingress_record_id, is_empty, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    source_report_version_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    provider_id,
                    provider_account_id,
                    provider_device_key,
                    local_report_date.isoformat(),
                    report_version,
                    content_sha256,
                    raw_ingress_record_id,
                    is_empty,
                    _dump_datetime(fetched_at),
                ),
            )
        return SourceReportVersion(
            source_report_version_id=source_report_version_id,
            provider_id=provider_id,
            provider_account_id=provider_account_id,
            provider_device_key=provider_device_key,
            local_report_date=local_report_date,
            report_version=report_version,
            content_sha256=content_sha256,
            raw_ingress_record_id=raw_ingress_record_id,
            is_empty=is_empty,
            fetched_at=fetched_at,
            created=True,
        )

    def get_source_report_version(
        self,
        namespace: DomainNamespace,
        *,
        source_report_version_id: str,
    ) -> SourceReportVersion | None:
        row = self._fetchone(
            """
            SELECT provider_id, provider_account_id, provider_device_key,
                   local_report_date, report_version, content_sha256,
                   raw_ingress_record_id, is_empty, fetched_at
            FROM sleep_domain_source_reports
            WHERE source_report_version_id = ? AND namespace_id = ?
              AND data_mode = ?
            """,
            (
                source_report_version_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return SourceReportVersion(
            source_report_version_id=source_report_version_id,
            provider_id=str(row[0]),
            provider_account_id=str(row[1]),
            provider_device_key=str(row[2]),
            local_report_date=date.fromisoformat(str(row[3])),
            report_version=int(row[4]),
            content_sha256=str(row[5]),
            raw_ingress_record_id=str(row[6]),
            is_empty=bool(row[7]),
            fetched_at=_parse_datetime(str(row[8])),
            created=False,
        )

    def get_pull_checkpoint(
        self,
        namespace: DomainNamespace,
        *,
        provider_id: str,
        provider_account_id: str,
        stream_key: str,
    ) -> PullCheckpoint | None:
        row = self._fetchone(
            """
            SELECT checkpoint_id, cursor_at, lateness_watermark_at,
                   cas_version, updated_at
            FROM sleep_domain_pull_checkpoints
            WHERE namespace_id = ? AND data_mode = ?
              AND provider_id = ? AND provider_account_id = ?
              AND stream_key = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                provider_id,
                provider_account_id,
                stream_key,
            ),
        )
        if row is None:
            return None
        return PullCheckpoint(
            checkpoint_id=str(row[0]),
            provider_id=provider_id,
            provider_account_id=provider_account_id,
            stream_key=stream_key,
            cursor_at=_parse_datetime(str(row[1])),
            lateness_watermark_at=_parse_datetime(str(row[2])),
            cas_version=int(row[3]),
            updated_at=_parse_datetime(str(row[4])),
        )

    def advance_pull_checkpoint(
        self,
        namespace: DomainNamespace,
        *,
        checkpoint_id: str,
        provider_id: str,
        provider_account_id: str,
        stream_key: str,
        cursor_at: datetime,
        lateness_watermark_at: datetime,
        expected_cas_version: int | None,
        updated_at: datetime,
    ) -> PullCheckpoint:
        """Create at CAS 0 or advance monotonically with compare-and-swap."""

        if lateness_watermark_at > cursor_at:
            raise ValueError("lateness watermark cannot be after cursor")
        with self._transaction(immediate=True) as cursor:
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT checkpoint_id, cursor_at, cas_version
                    FROM sleep_domain_pull_checkpoints
                    WHERE namespace_id = ? AND data_mode = ?
                      AND provider_id = ? AND provider_account_id = ?
                      AND stream_key = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    provider_id,
                    provider_account_id,
                    stream_key,
                ),
            ).fetchone()
            if existing is None:
                if expected_cas_version is not None:
                    raise CasConflictError("pull checkpoint does not exist")
                next_version = 0
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_pull_checkpoints (
                          checkpoint_id, namespace_id, data_mode, provider_id,
                          provider_account_id, stream_key, cursor_at,
                          lateness_watermark_at, cas_version, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        checkpoint_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        provider_id,
                        provider_account_id,
                        stream_key,
                        _dump_datetime(cursor_at),
                        _dump_datetime(lateness_watermark_at),
                        next_version,
                        _dump_datetime(updated_at),
                    ),
                )
            else:
                actual_version = int(existing[2])
                if expected_cas_version != actual_version:
                    raise CasConflictError("pull checkpoint CAS mismatch")
                if cursor_at < _parse_datetime(str(existing[1])):
                    raise ValueError("pull checkpoint cursor cannot move backward")
                next_version = actual_version + 1
                changed = cursor.execute(
                    self._sql(
                        """
                        UPDATE sleep_domain_pull_checkpoints
                        SET checkpoint_id = ?, cursor_at = ?,
                            lateness_watermark_at = ?, cas_version = ?,
                            updated_at = ?
                        WHERE namespace_id = ? AND data_mode = ?
                          AND provider_id = ? AND provider_account_id = ?
                          AND stream_key = ? AND cas_version = ?
                        """
                    ),
                    (
                        checkpoint_id,
                        _dump_datetime(cursor_at),
                        _dump_datetime(lateness_watermark_at),
                        next_version,
                        _dump_datetime(updated_at),
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        provider_id,
                        provider_account_id,
                        stream_key,
                        actual_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise CasConflictError("pull checkpoint CAS was lost")
        return PullCheckpoint(
            checkpoint_id=checkpoint_id,
            provider_id=provider_id,
            provider_account_id=provider_account_id,
            stream_key=stream_key,
            cursor_at=cursor_at,
            lateness_watermark_at=lateness_watermark_at,
            cas_version=next_version,
            updated_at=updated_at,
        )

    def _ingress_nonce_already_recorded(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        replay_guard: IngressReplayGuard,
    ) -> bool:
        nonce_row = cursor.execute(
            self._sql(
                """
                SELECT idempotency_identity,
                       pre_normalization_payload_sha256
                FROM sleep_domain_ingress_nonces
                WHERE namespace_id = ? AND data_mode = ?
                  AND provider_id = ? AND provider_account_id = ?
                  AND compatibility_profile_id = ? AND nonce = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                replay_guard.provider_id,
                replay_guard.provider_account_id,
                replay_guard.compatibility_profile_id,
                replay_guard.nonce,
            ),
        ).fetchone()
        if nonce_row is None:
            return False
        if (
            str(nonce_row[0]) != replay_guard.idempotency_identity
            or str(nonce_row[1])
            != replay_guard.pre_normalization_payload_sha256
        ):
            raise ReplayConflictError(
                "ingress nonce was already used by another request"
            )
        return True

    def _terminally_quarantine_work(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        work_id: str,
        terminal: TerminalRawQuarantine,
        completed_at: datetime,
    ) -> None:
        receipt = terminal.receipt
        detail_json = _dump_plain_json(dict(terminal.detail))
        work = cursor.execute(
            self._sql(
                """
                SELECT raw_ingress_record_id, status
                FROM sleep_domain_normalization_work
                WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                work_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if work is None or str(work[0]) != receipt.raw_ingress_record_id:
            raise KeyError("terminal quarantine work does not match raw record")
        existing = cursor.execute(
            self._sql(
                """
                SELECT raw_ingress_record_id, reason, detail_code,
                       receipt_id, quarantine_json
                FROM sleep_domain_quarantine
                WHERE quarantine_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                terminal.quarantine_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if str(work[1]) == "completed":
            if existing is None:
                raise ImmutableRecordConflictError(
                    "completed terminal work is missing its quarantine"
                )
            actual = (
                str(existing[0]),
                str(existing[1]),
                None if existing[2] is None else str(existing[2]),
                _canonical_json_text(existing[4]),
            )
            expected = (
                receipt.raw_ingress_record_id,
                receipt.quarantine_reason.value,
                receipt.detail_code,
                _canonical_json_text(detail_json),
            )
            if actual != expected:
                raise ImmutableRecordConflictError(
                    "terminal quarantine is append-only"
                )
            return
        if str(work[1]) != "pending":
            raise LeaseConflictError(
                "terminal quarantine cannot take over actively leased work"
            )
        self._insert_processing_receipt(cursor, namespace, receipt)
        if existing is None:
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_quarantine (
                      quarantine_id, namespace_id, data_mode,
                      raw_ingress_record_id, reason, detail_code,
                      receipt_id, quarantine_json, quarantined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    terminal.quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    receipt.raw_ingress_record_id,
                    receipt.quarantine_reason.value,
                    receipt.detail_code,
                    receipt.receipt_id,
                    detail_json,
                    _dump_datetime(receipt.occurred_at),
                ),
            )
        else:
            raise ImmutableRecordConflictError(
                "pending work already has terminal quarantine state"
            )
        cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_normalization_work
                SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                  AND status = 'pending'
                """
            ),
            (
                _dump_datetime(completed_at),
                work_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )

    def _insert_ingress_nonce(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        replay_guard: IngressReplayGuard,
        raw_ingress_record_id: str,
        recorded_at: datetime,
    ) -> None:
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_ingress_nonces (
                  namespace_id, data_mode, provider_id,
                  provider_account_id, compatibility_profile_id,
                  nonce, idempotency_identity,
                  pre_normalization_payload_sha256,
                  raw_ingress_record_id, request_signed_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                replay_guard.provider_id,
                replay_guard.provider_account_id,
                replay_guard.compatibility_profile_id,
                replay_guard.nonce,
                replay_guard.idempotency_identity,
                replay_guard.pre_normalization_payload_sha256,
                raw_ingress_record_id,
                _dump_datetime(replay_guard.request_signed_at),
                _dump_datetime(recorded_at),
            ),
        )

    def get_raw_record(
        self,
        namespace: DomainNamespace,
        *,
        raw_ingress_record_id: str,
    ) -> RawIngressRecord | None:
        row = self._fetchone(
            """
            SELECT raw_metadata_json
            FROM sleep_domain_raw_inbox
            WHERE raw_ingress_record_id = ? AND namespace_id = ?
              AND data_mode = ?
            """,
            (
                raw_ingress_record_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return RawIngressRecord.model_validate_json(_database_json_text(row[0]))

    def quarantine_normalization_work(
        self,
        namespace: DomainNamespace,
        *,
        work_id: str,
        worker_id: str | None,
        quarantine_id: str,
        receipt: ProcessingReceipt,
        detail: Mapping[str, JsonScalar],
        completed_at: datetime,
        require_active_lease: bool,
    ) -> None:
        """Atomically append raw quarantine state and terminally close its work."""

        self._require_mode(namespace, receipt.data_mode)
        if receipt.outcome != ProcessingOutcome.QUARANTINED:
            raise ValueError("raw work quarantine requires quarantined outcome")
        if receipt.quarantine_reason is None:
            raise ValueError("raw work quarantine requires a typed reason")
        detail_json = _dump_plain_json(dict(detail))
        with self._transaction(immediate=True) as cursor:
            work = cursor.execute(
                self._sql(
                    """
                    SELECT raw_ingress_record_id, status, lease_owner,
                           lease_expires_at
                    FROM sleep_domain_normalization_work
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if work is None or str(work[0]) != receipt.raw_ingress_record_id:
                raise KeyError("normalization work does not match raw record")
            if require_active_lease and (
                str(work[1]) != "leased"
                or str(work[2]) != worker_id
                or work[3] is None
                or _parse_datetime(str(work[3]))
                < completed_at.astimezone(timezone.utc)
            ):
                raise LeaseConflictError("normalization work lease is not active")
            if str(work[1]) == "completed":
                return
            self._insert_processing_receipt(cursor, namespace, receipt)
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT quarantine_json
                    FROM sleep_domain_quarantine
                    WHERE quarantine_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is None:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_quarantine (
                          quarantine_id, namespace_id, data_mode,
                          raw_ingress_record_id, reason, detail_code,
                          receipt_id, quarantine_json, quarantined_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        quarantine_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        receipt.raw_ingress_record_id,
                        receipt.quarantine_reason.value,
                        receipt.detail_code,
                        receipt.receipt_id,
                        detail_json,
                        _dump_datetime(receipt.occurred_at),
                    ),
                )
            elif not _json_equivalent(existing[0], detail_json):
                raise ImmutableRecordConflictError(
                    "quarantine record is append-only"
                )
            cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_normalization_work
                    SET status = 'completed', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    _dump_datetime(completed_at),
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            )

    def complete_normalization_work(
        self,
        namespace: DomainNamespace,
        *,
        work_id: str,
        worker_id: str,
        completed_at: datetime,
    ) -> bool:
        with self._transaction(immediate=True) as cursor:
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_normalization_work
                    SET status = 'completed', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                      AND status = 'leased' AND lease_owner = ?
                      AND lease_expires_at >= ?
                    """
                ),
                (
                    _dump_datetime(completed_at),
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    worker_id,
                    _dump_datetime(completed_at),
                ),
            ).rowcount
            return changed == 1

    def load_raw_payload(
        self,
        namespace: DomainNamespace,
        *,
        raw_ingress_record_id: str,
    ) -> bytes:
        row = self._fetchone(
            """
            SELECT encrypted_payload, encryption_key_id
            FROM sleep_domain_raw_inbox
            WHERE raw_ingress_record_id = ? AND namespace_id = ?
              AND data_mode = ?
            """,
            (
                raw_ingress_record_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            raise KeyError(f"raw ingress record not found: {raw_ingress_record_id}")
        ciphertext = bytes(row[0])
        return self.raw_payload_policy.decrypt(
            raw_ingress_record_id=raw_ingress_record_id,
            namespace_id=namespace.namespace_id,
            ciphertext=ciphertext,
            key_id=str(row[1]),
        )

    def append_processing_receipt(
        self,
        namespace: DomainNamespace,
        receipt: ProcessingReceipt,
    ) -> ProcessingReceipt:
        self._require_mode(namespace, receipt.data_mode)
        with self._transaction(immediate=True) as cursor:
            self._insert_processing_receipt(cursor, namespace, receipt)
        return receipt

    def append_adapter_candidate(
        self,
        namespace: DomainNamespace,
        candidate: AdapterObservationCandidate,
    ) -> AdapterObservationCandidate:
        """Persist a pre-binding candidate without assigning elder identity."""

        self._require_mode(namespace, candidate.data_mode)
        with self._transaction(immediate=True) as cursor:
            self._insert_candidate(cursor, namespace, candidate)
        return candidate

    def append_quarantine(
        self,
        namespace: DomainNamespace,
        *,
        quarantine_id: str,
        receipt: ProcessingReceipt,
        detail: Mapping[str, JsonScalar],
    ) -> ProcessingReceipt:
        self._require_mode(namespace, receipt.data_mode)
        if receipt.outcome != ProcessingOutcome.QUARANTINED:
            raise ValueError("quarantine requires a quarantined processing receipt")
        if receipt.quarantine_reason is None:
            raise ValueError("quarantine receipt requires a reason")
        with self._transaction(immediate=True) as cursor:
            self._insert_processing_receipt(cursor, namespace, receipt)
            detail_json = _dump_plain_json(dict(detail))
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT quarantine_json
                    FROM sleep_domain_quarantine
                    WHERE quarantine_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], detail_json):
                    raise ImmutableRecordConflictError(
                        "quarantine record is append-only"
                    )
                return receipt
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_quarantine (
                      quarantine_id, namespace_id, data_mode,
                      raw_ingress_record_id, reason, detail_code, receipt_id,
                      quarantine_json, quarantined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    receipt.raw_ingress_record_id,
                    receipt.quarantine_reason.value,
                    receipt.detail_code,
                    receipt.receipt_id,
                    detail_json,
                    _dump_datetime(receipt.occurred_at),
                ),
            )
        return receipt

    def commit_candidate_quarantine(
        self,
        namespace: DomainNamespace,
        *,
        candidate: AdapterObservationCandidate,
        quarantine_id: str,
        receipt: ProcessingReceipt,
        detail: Mapping[str, JsonScalar],
    ) -> QuarantineEntry:
        """Atomically retain a candidate, quarantine receipt, and immutable detail."""

        self._require_mode(namespace, candidate.data_mode)
        self._require_mode(namespace, receipt.data_mode)
        if receipt.outcome != ProcessingOutcome.QUARANTINED:
            raise ValueError("candidate quarantine requires quarantined outcome")
        if receipt.quarantine_reason is None:
            raise ValueError("candidate quarantine requires a typed reason")
        if (
            receipt.raw_ingress_record_id
            != candidate.provenance.raw_ingress_record_id
        ):
            raise ValueError("candidate and quarantine raw provenance must match")
        detail_json = _dump_plain_json(dict(detail))
        with self._transaction(immediate=True) as cursor:
            self._insert_candidate(cursor, namespace, candidate)
            self._insert_processing_receipt(cursor, namespace, receipt)
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT raw_ingress_record_id, reason, detail_code, receipt_id,
                           quarantine_json, quarantined_at
                    FROM sleep_domain_quarantine
                    WHERE quarantine_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                expected = (
                    receipt.raw_ingress_record_id,
                    receipt.quarantine_reason.value,
                    receipt.detail_code,
                    receipt.receipt_id,
                    _canonical_json_text(detail_json),
                )
                actual = (
                    str(existing[0]),
                    str(existing[1]),
                    None if existing[2] is None else str(existing[2]),
                    str(existing[3]),
                    _canonical_json_text(existing[4]),
                )
                if actual != expected:
                    raise ImmutableRecordConflictError(
                        "quarantine record is append-only"
                    )
                return QuarantineEntry(
                    quarantine_id=quarantine_id,
                    raw_ingress_record_id=actual[0],
                    reason=actual[1],
                    detail_code=actual[2],
                    receipt_id=actual[3],
                    detail=json.loads(_database_json_text(existing[4])),
                    quarantined_at=_parse_datetime(str(existing[5])),
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_quarantine (
                      quarantine_id, namespace_id, data_mode,
                      raw_ingress_record_id, reason, detail_code, receipt_id,
                      quarantine_json, quarantined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    quarantine_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    receipt.raw_ingress_record_id,
                    receipt.quarantine_reason.value,
                    receipt.detail_code,
                    receipt.receipt_id,
                    detail_json,
                    _dump_datetime(receipt.occurred_at),
                ),
            )
        return QuarantineEntry(
            quarantine_id=quarantine_id,
            raw_ingress_record_id=receipt.raw_ingress_record_id,
            reason=receipt.quarantine_reason.value,
            detail_code=receipt.detail_code,
            receipt_id=receipt.receipt_id,
            detail=dict(detail),
            quarantined_at=receipt.occurred_at,
        )

    def commit_candidate_observation(
        self,
        namespace: DomainNamespace,
        *,
        candidate: AdapterObservationCandidate,
        observation: SleepObservation,
        receipt: ProcessingReceipt,
        committed_at: datetime,
        fact_slot_key: str | None = None,
        fact_value_sha256: str | None = None,
        acquisition_channel: str | None = None,
    ) -> CandidatePromotionCommitResult:
        """Atomically promote one candidate without Episode/API side effects."""

        for mode in (candidate.data_mode, observation.data_mode, receipt.data_mode):
            self._require_mode(namespace, mode)
        raw_id = candidate.provenance.raw_ingress_record_id
        if observation.provenance.raw_ingress_record_id != raw_id:
            raise ValueError("candidate and observation raw provenance must match")
        if receipt.raw_ingress_record_id != raw_id:
            raise ValueError("promotion receipt must reference candidate raw record")
        if receipt.outcome not in {
            ProcessingOutcome.SUCCEEDED,
            ProcessingOutcome.RELEASED,
        }:
            raise ValueError("promotion requires succeeded or released receipt")
        fact_values = (fact_slot_key, fact_value_sha256, acquisition_channel)
        if any(value is not None for value in fact_values) and not all(
            isinstance(value, str) and value for value in fact_values
        ):
            raise ValueError(
                "fact slot, value hash, and acquisition channel are all required"
            )
        if fact_value_sha256 is not None and (
            len(fact_value_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in fact_value_sha256)
        ):
            raise ValueError("fact value hash must be lowercase SHA-256")
        observation_json = observation.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            self._insert_candidate(cursor, namespace, candidate)
            if fact_slot_key is not None and fact_value_sha256 is not None:
                matching_fact = cursor.execute(
                    self._sql(
                        """
                        SELECT observation_id
                        FROM sleep_domain_observation_fact_values
                        WHERE namespace_id = ? AND data_mode = ?
                          AND fact_slot_key = ? AND value_sha256 = ?
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        fact_slot_key,
                        fact_value_sha256,
                    ),
                ).fetchone()
                if matching_fact is not None:
                    retained_observation_id = str(matching_fact[0])
                    self._insert_processing_receipt(cursor, namespace, receipt)
                    self._insert_observation_acquisition(
                        cursor,
                        namespace,
                        candidate=candidate,
                        observation_id=retained_observation_id,
                        fact_slot_key=fact_slot_key,
                        fact_value_sha256=fact_value_sha256,
                        acquisition_channel=str(acquisition_channel),
                        acquired_at=committed_at,
                    )
                    return CandidatePromotionCommitResult(
                        observation_id=retained_observation_id,
                        created=False,
                    )
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT observation_json
                    FROM sleep_domain_canonical_observations
                    WHERE observation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    observation.observation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], observation_json):
                    raise ImmutableRecordConflictError(
                        "canonical observation is immutable"
                    )
                self._insert_processing_receipt(cursor, namespace, receipt)
                if fact_slot_key is not None and fact_value_sha256 is not None:
                    self._insert_observation_acquisition(
                        cursor,
                        namespace,
                        candidate=candidate,
                        observation_id=observation.observation_id,
                        fact_slot_key=fact_slot_key,
                        fact_value_sha256=fact_value_sha256,
                        acquisition_channel=str(acquisition_channel),
                        acquired_at=committed_at,
                    )
                return CandidatePromotionCommitResult(
                    observation_id=observation.observation_id,
                    created=False,
                )
            self._validate_observation_binding(cursor, namespace, observation)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_canonical_observations (
                      observation_id, namespace_id, data_mode, candidate_id,
                      raw_ingress_record_id, subject_id, device_id,
                      device_binding_id, binding_version, observation_type,
                      source_key, idempotency_key, observation_json,
                      measurement_at, event_occurred_at, received_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    observation.observation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    candidate.candidate_id,
                    raw_id,
                    observation.subject_id,
                    observation.device_id,
                    observation.device_binding_id,
                    observation.binding_version,
                    observation.observation_type.value,
                    observation.source_key,
                    observation.idempotency_key,
                    observation_json,
                    _dump_optional_datetime(observation.measurement_at),
                    _dump_optional_datetime(observation.event_occurred_at),
                    _dump_datetime(observation.received_at),
                    _dump_datetime(committed_at),
                ),
            )
            if fact_slot_key is not None and fact_value_sha256 is not None:
                fact_value_id = "fact-value:" + hashlib.sha256(
                    (
                        f"{namespace.namespace_id}|{fact_slot_key}|"
                        f"{fact_value_sha256}"
                    ).encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_observation_fact_values (
                          fact_value_id, namespace_id, data_mode,
                          fact_slot_key, value_sha256, observation_id,
                          candidate_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        fact_value_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        fact_slot_key,
                        fact_value_sha256,
                        observation.observation_id,
                        candidate.candidate_id,
                        _dump_datetime(committed_at),
                    ),
                )
                conflicting = cursor.execute(
                    self._sql(
                        """
                        SELECT value_sha256, observation_id
                        FROM sleep_domain_observation_fact_values
                        WHERE namespace_id = ? AND data_mode = ?
                          AND fact_slot_key = ? AND value_sha256 <> ?
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        fact_slot_key,
                        fact_value_sha256,
                    ),
                ).fetchall()
                for conflicting_hash, conflicting_observation_id in conflicting:
                    first_hash, second_hash = sorted(
                        (str(conflicting_hash), fact_value_sha256)
                    )
                    observation_by_hash = {
                        str(conflicting_hash): str(conflicting_observation_id),
                        fact_value_sha256: observation.observation_id,
                    }
                    conflict_id = "observation-conflict:" + hashlib.sha256(
                        (
                            f"{namespace.namespace_id}|{fact_slot_key}|"
                            f"{first_hash}|{second_hash}"
                        ).encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO sleep_domain_observation_conflicts (
                              conflict_id, namespace_id, data_mode,
                              fact_slot_key, first_value_sha256,
                              second_value_sha256, first_observation_id,
                              second_observation_id, detected_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (
                              namespace_id, data_mode, fact_slot_key,
                              first_value_sha256, second_value_sha256
                            ) DO NOTHING
                            """
                        ),
                        (
                            conflict_id,
                            namespace.namespace_id,
                            namespace.data_mode.value,
                            fact_slot_key,
                            first_hash,
                            second_hash,
                            observation_by_hash[first_hash],
                            observation_by_hash[second_hash],
                            _dump_datetime(committed_at),
                        ),
                    )
                self._insert_observation_acquisition(
                    cursor,
                    namespace,
                    candidate=candidate,
                    observation_id=observation.observation_id,
                    fact_slot_key=fact_slot_key,
                    fact_value_sha256=fact_value_sha256,
                    acquisition_channel=str(acquisition_channel),
                    acquired_at=committed_at,
                )
            self._insert_processing_receipt(cursor, namespace, receipt)
        return CandidatePromotionCommitResult(
            observation_id=observation.observation_id,
            created=True,
        )

    def get_observation(
        self,
        namespace: DomainNamespace,
        *,
        observation_id: str,
    ) -> SleepObservation | None:
        row = self._fetchone(
            """
            SELECT observation_json
            FROM sleep_domain_canonical_observations
            WHERE observation_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                observation_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return SleepObservation.model_validate_json(_database_json_text(row[0]))

    def list_observation_conflicts(
        self,
        namespace: DomainNamespace,
        *,
        observation_ids: tuple[str, ...],
    ) -> tuple[ObservationConflictRecord, ...]:
        """Return only conflicts whose two facts are inside the requested scope."""

        scoped_ids = tuple(dict.fromkeys(observation_ids))
        if not scoped_ids:
            return ()
        placeholders = ", ".join("?" for _ in scoped_ids)
        rows = self._fetchall(
            f"""
            SELECT conflict_id, fact_slot_key, first_observation_id,
                   second_observation_id, detected_at
            FROM sleep_domain_observation_conflicts
            WHERE namespace_id = ? AND data_mode = ?
              AND first_observation_id IN ({placeholders})
              AND second_observation_id IN ({placeholders})
            ORDER BY detected_at, conflict_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                *scoped_ids,
                *scoped_ids,
            ),
        )
        return tuple(
            ObservationConflictRecord(
                conflict_id=str(row[0]),
                fact_slot_key=str(row[1]),
                first_observation_id=str(row[2]),
                second_observation_id=str(row[3]),
                detected_at=_parse_datetime(str(row[4])),
            )
            for row in rows
        )

    def get_candidate(
        self,
        namespace: DomainNamespace,
        *,
        candidate_id: str,
    ) -> AdapterObservationCandidate | None:
        row = self._fetchone(
            """
            SELECT candidate_json
            FROM sleep_domain_adapter_candidates
            WHERE candidate_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                candidate_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return AdapterObservationCandidate.model_validate_json(
            _database_json_text(row[0])
        )

    def get_quarantine(
        self,
        namespace: DomainNamespace,
        *,
        quarantine_id: str,
    ) -> QuarantineEntry | None:
        row = self._fetchone(
            """
            SELECT raw_ingress_record_id, reason, detail_code, receipt_id,
                   quarantine_json, quarantined_at
            FROM sleep_domain_quarantine
            WHERE quarantine_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                quarantine_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return QuarantineEntry(
            quarantine_id=quarantine_id,
            raw_ingress_record_id=str(row[0]),
            reason=str(row[1]),
            detail_code=None if row[2] is None else str(row[2]),
            receipt_id=str(row[3]),
            detail=json.loads(_database_json_text(row[4])),
            quarantined_at=_parse_datetime(str(row[5])),
        )

    def append_quarantine_reprocess_audit(
        self,
        namespace: DomainNamespace,
        audit: QuarantineReprocessAudit,
    ) -> QuarantineReprocessAudit:
        self._require_mode(namespace, audit.data_mode)
        audit_json = audit.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT request_json
                    FROM sleep_domain_quarantine_reprocess_audit
                    WHERE request_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    audit.request_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], audit_json):
                    raise ImmutableRecordConflictError(
                        "quarantine reprocess request is immutable"
                    )
                return QuarantineReprocessAudit.model_validate_json(
                    _database_json_text(existing[0])
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_quarantine_reprocess_audit (
                      request_id, namespace_id, data_mode, actor_id,
                      authorization_id, request_json, requested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    audit.request_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    audit.actor_id,
                    audit.authorization_id,
                    audit_json,
                    _dump_datetime(audit.requested_at),
                ),
            )
        return audit

    def lease_normalization_work(
        self,
        namespace: DomainNamespace,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> NormalizationWorkLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        now_text = _dump_datetime(now)
        lease_expires_at = now + lease_duration
        with self._transaction(immediate=True) as cursor:
            suffix = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
            row = cursor.execute(
                self._sql(
                    """
                    SELECT work_id, raw_ingress_record_id, work_generation,
                           attempt_count, work_json
                    FROM sleep_domain_normalization_work
                    WHERE namespace_id = ? AND data_mode = ?
                      AND (
                        (status IN ('pending', 'retryable_failed')
                         AND available_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                      )
                    ORDER BY available_at, created_at, work_id
                    LIMIT 1
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    now_text,
                    now_text,
                ),
            ).fetchone()
            if row is None:
                return None
            next_attempt = int(row[3]) + 1
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_normalization_work
                    SET status = 'leased', attempt_count = ?,
                        lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                      AND attempt_count = ?
                      AND (
                        (status IN ('pending', 'retryable_failed')
                         AND available_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                      )
                    """
                ),
                (
                    next_attempt,
                    worker_id,
                    _dump_datetime(lease_expires_at),
                    now_text,
                    str(row[0]),
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    int(row[3]),
                    now_text,
                    now_text,
                ),
            ).rowcount
            if changed != 1:
                return None
            return NormalizationWorkLease(
                work_id=str(row[0]),
                raw_ingress_record_id=str(row[1]),
                work_generation=int(row[2]),
                attempt_count=next_attempt,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                work_json=_database_json_text(row[4]),
            )

    def commit_normalization(
        self,
        namespace: DomainNamespace,
        *,
        work_id: str,
        worker_id: str,
        candidate: AdapterObservationCandidate,
        observation: SleepObservation,
        processing_receipt: ProcessingReceipt,
        domain_event: DomainEvent,
        night_episode: NightEpisode | None,
        night_episode_expected_cas_version: int | None = None,
        committed_at: datetime,
    ) -> NormalizationCommitResult:
        """Commit canonical fact + affected state + outbox in one transaction."""

        for mode in (
            candidate.data_mode,
            observation.data_mode,
            processing_receipt.data_mode,
            domain_event.data_mode,
        ):
            self._require_mode(namespace, mode)
        if night_episode is not None:
            self._require_mode(namespace, night_episode.data_mode)
        raw_id = candidate.provenance.raw_ingress_record_id
        if observation.provenance.raw_ingress_record_id != raw_id:
            raise ValueError("candidate and observation raw provenance must match")
        if processing_receipt.raw_ingress_record_id != raw_id:
            raise ValueError("processing receipt must reference normalized raw record")
        if processing_receipt.outcome != ProcessingOutcome.SUCCEEDED:
            raise ValueError("normalization commit requires a succeeded receipt")
        if domain_event.subject_id != observation.subject_id:
            raise ValueError("domain event subject must match observation")
        if (
            night_episode is not None
            and night_episode.subject_id != observation.subject_id
        ):
            raise ValueError("NightEpisode subject must match observation")

        candidate_json = candidate.model_dump_json()
        observation_json = observation.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            existing_observation = cursor.execute(
                self._sql(
                    """
                    SELECT observation_json
                    FROM sleep_domain_canonical_observations
                    WHERE observation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    observation.observation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            existing_event = cursor.execute(
                self._sql(
                    """
                    SELECT event_json
                    FROM sleep_domain_domain_outbox
                    WHERE event_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    domain_event.event_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing_observation is not None or existing_event is not None:
                if existing_observation is None or existing_event is None:
                    raise SleepDomainPersistenceError(
                        "partial normalization commit detected"
                    )
                stored_observation = _database_json_text(existing_observation[0])
                stored_event = DomainEvent.model_validate_json(
                    _database_json_text(existing_event[0])
                )
                comparable_event = domain_event.model_copy(
                    update={"delivery_offset": stored_event.delivery_offset}
                )
                if (
                    not _json_equivalent(stored_observation, observation_json)
                    or stored_event != comparable_event
                ):
                    raise ImmutableRecordConflictError(
                        "normalization idempotency identity has different content"
                    )
                return NormalizationCommitResult(
                    observation_id=observation.observation_id,
                    event=stored_event,
                    created=False,
                )

            work = cursor.execute(
                self._sql(
                    """
                    SELECT status, lease_owner, lease_expires_at
                    FROM sleep_domain_normalization_work
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if (
                work is None
                or str(work[0]) != "leased"
                or str(work[1]) != worker_id
                or _parse_datetime(str(work[2])) < committed_at.astimezone(timezone.utc)
            ):
                raise LeaseConflictError("normalization work lease is not active")

            self._insert_candidate(cursor, namespace, candidate, candidate_json)
            self._validate_observation_binding(cursor, namespace, observation)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_canonical_observations (
                      observation_id, namespace_id, data_mode, candidate_id,
                      raw_ingress_record_id, subject_id, device_id,
                      device_binding_id, binding_version, observation_type,
                      source_key, idempotency_key, observation_json,
                      measurement_at, event_occurred_at, received_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    observation.observation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    candidate.candidate_id,
                    raw_id,
                    observation.subject_id,
                    observation.device_id,
                    observation.device_binding_id,
                    observation.binding_version,
                    observation.observation_type.value,
                    observation.source_key,
                    observation.idempotency_key,
                    observation_json,
                    _dump_optional_datetime(observation.measurement_at),
                    _dump_optional_datetime(observation.event_occurred_at),
                    _dump_datetime(observation.received_at),
                    _dump_datetime(committed_at),
                ),
            )
            if night_episode is not None:
                self._write_night_episode(
                    cursor,
                    namespace,
                    night_episode,
                    expected_cas_version=night_episode_expected_cas_version,
                )
            self._insert_processing_receipt(
                cursor,
                namespace,
                processing_receipt,
            )
            stored_event = self._insert_domain_event(
                cursor,
                namespace,
                domain_event,
            )
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_normalization_work
                    SET status = 'completed', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE work_id = ? AND namespace_id = ? AND data_mode = ?
                      AND status = 'leased' AND lease_owner = ?
                    """
                ),
                (
                    _dump_datetime(committed_at),
                    work_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    worker_id,
                ),
            ).rowcount
            if changed != 1:
                raise LeaseConflictError("normalization work lease changed")
        return NormalizationCommitResult(
            observation_id=observation.observation_id,
            event=stored_event,
            created=True,
        )

    def create_night_episode(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
    ) -> NightEpisode:
        self._require_mode(namespace, episode.data_mode)
        with self._transaction(immediate=True) as cursor:
            self._insert_night_episode(cursor, namespace, episode)
        return episode

    def get_night_episode(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> NightEpisode | None:
        row = self._fetchone(
            """
            SELECT episode_json
            FROM sleep_domain_night_episodes
            WHERE night_episode_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return NightEpisode.model_validate_json(_database_json_text(row[0]))

    def list_night_episodes(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
    ) -> tuple[NightEpisode, ...]:
        rows = self._fetchall(
            """
            SELECT episode_json
            FROM sleep_domain_night_episodes
            WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
            ORDER BY created_at, night_episode_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                subject_id,
            ),
        )
        return tuple(
            NightEpisode.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def get_night_episode_revision(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_revision_id: str,
    ) -> NightEpisodeRevision | None:
        row = self._fetchone(
            """
            SELECT revision_json
            FROM sleep_domain_night_episode_revisions
            WHERE night_episode_revision_id = ? AND namespace_id = ?
              AND data_mode = ?
            """,
            (
                night_episode_revision_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return NightEpisodeRevision.model_validate_json(
            _database_json_text(row[0])
        )

    def get_monitoring_snapshot(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
    ) -> MonitoringSnapshot | None:
        row = self._fetchone(
            """
            SELECT snapshot_json
            FROM sleep_domain_monitoring_snapshots
            WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                subject_id,
            ),
        )
        if row is None:
            return None
        return MonitoringSnapshot.model_validate_json(
            _database_json_text(row[0])
        )

    def get_lifecycle_transition_receipt(
        self,
        namespace: DomainNamespace,
        *,
        component: str,
        aggregate_id: str,
        trigger_id: str,
    ) -> LifecycleTransitionReceipt | None:
        row = self._fetchone(
            """
            SELECT receipt_json
            FROM sleep_domain_lifecycle_transition_receipts
            WHERE namespace_id = ? AND data_mode = ? AND component = ?
              AND aggregate_id = ? AND trigger_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                component,
                aggregate_id,
                trigger_id,
            ),
        )
        if row is None:
            return None
        return LifecycleTransitionReceipt.model_validate_json(
            _database_json_text(row[0])
        )

    def acquire_subject_lifecycle_lease(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        lease_owner: str,
        lease_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> SubjectLifecycleLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        expires_at = now + lease_duration
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT lease_owner, lease_token, lease_expires_at
                    FROM sleep_domain_subject_lifecycle_leases
                    WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    subject_id,
                ),
            ).fetchone()
            if row is None:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_subject_lifecycle_leases (
                          namespace_id, data_mode, subject_id, lease_owner,
                          lease_token, lease_expires_at, acquired_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        subject_id,
                        lease_owner,
                        lease_token,
                        _dump_datetime(expires_at),
                        _dump_datetime(now),
                    ),
                )
            else:
                stored_expiry = _parse_datetime(str(row[2]))
                if stored_expiry > now.astimezone(timezone.utc):
                    if str(row[1]) != lease_token:
                        return None
                    return SubjectLifecycleLease(
                        subject_id=subject_id,
                        lease_owner=str(row[0]),
                        lease_token=str(row[1]),
                        lease_expires_at=stored_expiry,
                    )
                changed = cursor.execute(
                    self._sql(
                        """
                        UPDATE sleep_domain_subject_lifecycle_leases
                        SET lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?, acquired_at = ?
                        WHERE namespace_id = ? AND data_mode = ?
                          AND subject_id = ? AND lease_expires_at <= ?
                        """
                    ),
                    (
                        lease_owner,
                        lease_token,
                        _dump_datetime(expires_at),
                        _dump_datetime(now),
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        subject_id,
                        _dump_datetime(now),
                    ),
                ).rowcount
                if changed != 1:
                    return None
        return SubjectLifecycleLease(
            subject_id=subject_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=expires_at,
        )

    def release_subject_lifecycle_lease(
        self,
        namespace: DomainNamespace,
        lease: SubjectLifecycleLease,
    ) -> bool:
        with self._transaction(immediate=True) as cursor:
            changed = cursor.execute(
                self._sql(
                    """
                    DELETE FROM sleep_domain_subject_lifecycle_leases
                    WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
                      AND lease_owner = ? AND lease_token = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    lease.subject_id,
                    lease.lease_owner,
                    lease.lease_token,
                ),
            ).rowcount
        return changed == 1

    def next_domain_event_sequence(
        self,
        namespace: DomainNamespace,
        *,
        aggregate_type: str,
        aggregate_id: str,
    ) -> int:
        row = self._fetchone(
            """
            SELECT COALESCE(MAX(per_aggregate_sequence), 0)
            FROM sleep_domain_domain_outbox
            WHERE namespace_id = ? AND data_mode = ? AND aggregate_type = ?
              AND aggregate_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                aggregate_type,
                aggregate_id,
            ),
        )
        return int(row[0]) + 1

    def commit_lifecycle_transition(
        self,
        namespace: DomainNamespace,
        *,
        monitoring_snapshot: MonitoringSnapshot | None,
        expected_monitoring_cas: int | None,
        episode: NightEpisode | None,
        expected_episode_cas: int | None,
        receipts: tuple[LifecycleTransitionReceipt, ...],
        events: tuple[DomainEvent, ...],
        memberships: tuple[EpisodeObservationMembership, ...] = (),
    ) -> None:
        """Commit one deterministic transition batch and all outbox events."""

        for receipt in receipts:
            self._require_mode(namespace, receipt.data_mode)
        for event in events:
            self._require_mode(namespace, event.data_mode)
        if monitoring_snapshot is not None:
            self._require_mode(namespace, monitoring_snapshot.data_mode)
        if episode is not None:
            self._require_mode(namespace, episode.data_mode)
        if len(events) < len(receipts):
            raise ValueError("every lifecycle transition requires an outbox event")
        with self._transaction(immediate=True) as cursor:
            if episode is not None:
                self._write_night_episode(
                    cursor,
                    namespace,
                    episode,
                    expected_cas_version=expected_episode_cas,
                )
            if monitoring_snapshot is not None:
                self._write_monitoring_snapshot(
                    cursor,
                    namespace,
                    monitoring_snapshot,
                    expected_cas_version=expected_monitoring_cas,
                )
            for membership in memberships:
                self._insert_episode_observation_membership(
                    cursor,
                    namespace,
                    membership,
                )
            for receipt in receipts:
                self._insert_lifecycle_transition_receipt(
                    cursor,
                    namespace,
                    receipt,
                )
            for event in events:
                self._insert_domain_event(cursor, namespace, event)

    def append_pending_episode_association(
        self,
        namespace: DomainNamespace,
        association: PendingEpisodeAssociation,
    ) -> PendingEpisodeAssociation:
        self._require_mode(namespace, association.data_mode)
        association_json = association.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT association_json
                    FROM sleep_domain_pending_episode_associations
                    WHERE namespace_id = ? AND data_mode = ?
                      AND association_kind = ? AND source_resource_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    association.association_kind.value,
                    association.source_resource_id,
                ),
            ).fetchone()
            if row is not None:
                return PendingEpisodeAssociation.model_validate_json(
                    _database_json_text(row[0])
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_pending_episode_associations (
                      association_id, namespace_id, data_mode,
                      association_kind, source_resource_id, subject_id,
                      status, reason_code, association_json, created_at,
                      resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    association.association_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    association.association_kind.value,
                    association.source_resource_id,
                    association.subject_id,
                    association.status.value,
                    association.reason_code,
                    association_json,
                    _dump_datetime(association.created_at),
                    _dump_optional_datetime(association.resolved_at),
                ),
            )
        return association

    def get_pending_episode_association(
        self,
        namespace: DomainNamespace,
        *,
        association_kind: str,
        source_resource_id: str,
    ) -> PendingEpisodeAssociation | None:
        row = self._fetchone(
            """
            SELECT association_json
            FROM sleep_domain_pending_episode_associations
            WHERE namespace_id = ? AND data_mode = ? AND association_kind = ?
              AND source_resource_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                association_kind,
                source_resource_id,
            ),
        )
        if row is None:
            return None
        return PendingEpisodeAssociation.model_validate_json(
            _database_json_text(row[0])
        )

    def append_night_episode_revision(
        self,
        namespace: DomainNamespace,
        revision: NightEpisodeRevision,
    ) -> NightEpisodeRevision:
        self._require_mode(namespace, revision.data_mode)
        revision_json = revision.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            episode = cursor.execute(
                self._sql(
                    """
                    SELECT subject_id
                    FROM sleep_domain_night_episodes
                    WHERE night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    revision.night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if episode is None:
                raise KeyError(
                    f"NightEpisode not found: {revision.night_episode_id}"
                )
            if str(episode[0]) != revision.subject_id:
                raise NamespaceMismatchError("revision subject does not match episode")
            if revision.parent_revision_id is not None:
                parent = cursor.execute(
                    self._sql(
                        """
                        SELECT revision_number
                        FROM sleep_domain_night_episode_revisions
                        WHERE night_episode_revision_id = ?
                          AND night_episode_id = ? AND namespace_id = ?
                          AND data_mode = ?
                        """
                    ),
                    (
                        revision.parent_revision_id,
                        revision.night_episode_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                if parent is None or int(parent[0]) != revision.revision_number - 1:
                    raise ValueError(
                        "parent revision must be the prior revision of this episode"
                    )
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT revision_json
                    FROM sleep_domain_night_episode_revisions
                    WHERE night_episode_revision_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    revision.night_episode_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], revision_json):
                    raise ImmutableRecordConflictError(
                        "NightEpisodeRevision is append-only"
                    )
                return revision
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_night_episode_revisions (
                      night_episode_revision_id, namespace_id, data_mode,
                      night_episode_id, subject_id, revision_number,
                      parent_revision_id, revision_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    revision.night_episode_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    revision.night_episode_id,
                    revision.subject_id,
                    revision.revision_number,
                    revision.parent_revision_id,
                    revision_json,
                    _dump_datetime(revision.created_at),
                ),
            )
        return revision

    def append_analysis_revision(
        self,
        namespace: DomainNamespace,
        revision: AnalysisRevision,
    ) -> AnalysisRevision:
        self._require_mode(namespace, revision.data_mode)
        revision_json = revision.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            parent = cursor.execute(
                self._sql(
                    """
                    SELECT subject_id
                    FROM sleep_domain_night_episode_revisions
                    WHERE night_episode_revision_id = ?
                      AND night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    revision.night_episode_revision_id,
                    revision.night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if parent is None or str(parent[0]) != revision.subject_id:
                raise ValueError(
                    "analysis must reference an exact matching NightEpisode revision"
                )
            if revision.parent_analysis_revision_id is not None:
                parent_analysis = cursor.execute(
                    self._sql(
                        """
                        SELECT revision_number
                        FROM sleep_domain_analysis_revisions
                        WHERE analysis_revision_id = ?
                          AND night_episode_revision_id = ?
                          AND namespace_id = ? AND data_mode = ?
                        """
                    ),
                    (
                        revision.parent_analysis_revision_id,
                        revision.night_episode_revision_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                if (
                    parent_analysis is None
                    or int(parent_analysis[0]) != revision.revision_number - 1
                ):
                    raise ValueError(
                        "parent analysis must be the prior analysis revision"
                    )
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT analysis_json
                    FROM sleep_domain_analysis_revisions
                    WHERE analysis_revision_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    revision.analysis_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], revision_json):
                    raise ImmutableRecordConflictError(
                        "AnalysisRevision is append-only"
                    )
                return revision
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_analysis_revisions (
                      analysis_revision_id, namespace_id, data_mode,
                      night_episode_id, night_episode_revision_id, subject_id,
                      revision_number, parent_analysis_revision_id,
                      analysis_run_id, analysis_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    revision.analysis_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    revision.night_episode_id,
                    revision.night_episode_revision_id,
                    revision.subject_id,
                    revision.revision_number,
                    revision.parent_analysis_revision_id,
                    revision.analysis_run_id,
                    revision_json,
                    _dump_datetime(revision.created_at),
                ),
            )
        return revision

    def get_analysis_revision(
        self,
        namespace: DomainNamespace,
        *,
        analysis_revision_id: str,
    ) -> AnalysisRevision | None:
        row = self._fetchone(
            """
            SELECT analysis_json
            FROM sleep_domain_analysis_revisions
            WHERE analysis_revision_id = ? AND namespace_id = ?
              AND data_mode = ?
            """,
            (
                analysis_revision_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return AnalysisRevision.model_validate_json(_database_json_text(row[0]))

    def list_analysis_revisions(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_revision_id: str,
    ) -> tuple[AnalysisRevision, ...]:
        rows = self._fetchall(
            """
            SELECT analysis_json
            FROM sleep_domain_analysis_revisions
            WHERE namespace_id = ? AND data_mode = ?
              AND night_episode_revision_id = ?
            ORDER BY revision_number, created_at, analysis_revision_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_revision_id,
            ),
        )
        return tuple(
            AnalysisRevision.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def get_analysis_role_view(
        self,
        namespace: DomainNamespace,
        *,
        analysis_revision_id: str,
        role: AnalysisRole,
    ) -> AnalysisRoleView | None:
        row = self._fetchone(
            """
            SELECT view_json
            FROM sleep_domain_analysis_role_views
            WHERE namespace_id = ? AND data_mode = ?
              AND analysis_revision_id = ? AND role = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                analysis_revision_id,
                role.value,
            ),
        )
        if row is None:
            return None
        return AnalysisRoleView.model_validate_json(_database_json_text(row[0]))

    def append_analysis_role_view(
        self,
        namespace: DomainNamespace,
        view: AnalysisRoleView,
    ) -> AnalysisRoleView:
        self._require_mode(namespace, view.data_mode)
        with self._transaction(immediate=True) as cursor:
            parent = cursor.execute(
                self._sql(
                    """
                    SELECT subject_id, night_episode_id,
                           night_episode_revision_id
                    FROM sleep_domain_analysis_revisions
                    WHERE analysis_revision_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    view.analysis_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if parent is None or (
                str(parent[0]),
                str(parent[1]),
                str(parent[2]),
            ) != (
                view.subject_id,
                view.night_episode_id,
                view.night_episode_revision_id,
            ):
                raise ValueError("role view is not bound to its analysis revision")
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT view_json
                    FROM sleep_domain_analysis_role_views
                    WHERE namespace_id = ? AND data_mode = ?
                      AND analysis_revision_id = ? AND role = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    view.analysis_revision_id,
                    view.role.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(existing[0], view.model_dump_json()):
                    raise ImmutableRecordConflictError(
                        "AnalysisRoleView is append-only"
                    )
                return AnalysisRoleView.model_validate_json(
                    _database_json_text(existing[0])
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_analysis_role_views (
                      role_view_id, namespace_id, data_mode,
                      analysis_revision_id, night_episode_id,
                      night_episode_revision_id, subject_id, role, status,
                      product_agent_episode_id, view_json, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    view.role_view_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    view.analysis_revision_id,
                    view.night_episode_id,
                    view.night_episode_revision_id,
                    view.subject_id,
                    view.role.value,
                    view.status.value,
                    view.product_agent_episode_id,
                    view.model_dump_json(),
                    _dump_datetime(view.generated_at),
                ),
            )
        return view

    def list_analysis_role_views(
        self,
        namespace: DomainNamespace,
        *,
        analysis_revision_id: str,
    ) -> tuple[AnalysisRoleView, ...]:
        rows = self._fetchall(
            """
            SELECT view_json
            FROM sleep_domain_analysis_role_views
            WHERE namespace_id = ? AND data_mode = ?
              AND analysis_revision_id = ?
            ORDER BY role
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                analysis_revision_id,
            ),
        )
        return tuple(
            AnalysisRoleView.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def compare_and_set_current_night_revision(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        expected_cas_version: int,
        expected_current_revision_id: str | None,
        next_revision_id: str,
        updated_at: datetime,
    ) -> bool:
        with self._transaction(immediate=True) as cursor:
            revision = cursor.execute(
                self._sql(
                    """
                    SELECT revision_number
                    FROM sleep_domain_night_episode_revisions
                    WHERE night_episode_revision_id = ?
                      AND night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    next_revision_id,
                    night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if revision is None:
                raise ValueError("next current revision does not belong to episode")
            episode_row = cursor.execute(
                self._sql(
                    """
                    SELECT episode_json
                    FROM sleep_domain_night_episodes
                    WHERE night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if episode_row is None:
                raise KeyError(f"NightEpisode not found: {night_episode_id}")
            episode = NightEpisode.model_validate_json(
                _database_json_text(episode_row[0])
            )
            updated_episode = NightEpisode.model_validate(
                {
                    **episode.model_dump(mode="python"),
                    "current_night_episode_revision_id": next_revision_id,
                    "updated_at": updated_at,
                }
            )
            pointer_clause = (
                "current_revision_id IS NULL"
                if expected_current_revision_id is None
                else "current_revision_id = ?"
            )
            params: list[Any] = [
                next_revision_id,
                int(revision[0]),
                expected_cas_version + 1,
                updated_episode.model_dump_json(),
                _dump_datetime(updated_at),
                night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                expected_cas_version,
            ]
            if expected_current_revision_id is not None:
                params.append(expected_current_revision_id)
            changed = cursor.execute(
                self._sql(
                    f"""
                    UPDATE sleep_domain_night_episodes
                    SET current_revision_id = ?, current_revision_number = ?,
                        cas_version = ?, episode_json = ?, updated_at = ?
                    WHERE night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                      AND {pointer_clause}
                    """
                ),
                tuple(params),
            ).rowcount
            return changed == 1

    def get_current_night_revision(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> CurrentRevisionPointer:
        row = self._fetchone(
            """
            SELECT current_revision_id, current_revision_number, cas_version
            FROM sleep_domain_night_episodes
            WHERE night_episode_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            raise KeyError(f"NightEpisode not found: {night_episode_id}")
        return CurrentRevisionPointer(
            night_episode_id=night_episode_id,
            current_revision_id=None if row[0] is None else str(row[0]),
            current_revision_number=None if row[1] is None else int(row[1]),
            cas_version=int(row[2]),
        )

    def get_episode_observation_membership(
        self,
        namespace: DomainNamespace,
        *,
        observation_id: str,
    ) -> EpisodeObservationMembership | None:
        row = self._fetchone(
            """
            SELECT membership_id, night_episode_id, subject_id,
                   device_binding_id, binding_version, event_at, received_at,
                   lateness_watermark_at, late_after_watermark, associated_at
            FROM sleep_domain_episode_observation_memberships
            WHERE namespace_id = ? AND data_mode = ? AND observation_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                observation_id,
            ),
        )
        if row is None:
            return None
        return EpisodeObservationMembership(
            membership_id=str(row[0]),
            night_episode_id=str(row[1]),
            observation_id=observation_id,
            subject_id=str(row[2]),
            device_binding_id=str(row[3]),
            binding_version=int(row[4]),
            event_at=_parse_datetime(str(row[5])),
            received_at=_parse_datetime(str(row[6])),
            lateness_watermark_at=(
                None if row[7] is None else _parse_datetime(str(row[7]))
            ),
            late_after_watermark=bool(row[8]),
            associated_at=_parse_datetime(str(row[9])),
        )

    def commit_episode_observation_membership(
        self,
        namespace: DomainNamespace,
        *,
        episode: NightEpisode,
        expected_episode_cas: int,
        membership: EpisodeObservationMembership,
        pending_association: PendingEpisodeAssociation | None = None,
    ) -> bool:
        self._require_mode(namespace, episode.data_mode)
        if (
            episode.night_episode_id != membership.night_episode_id
            or episode.subject_id != membership.subject_id
            or membership.observation_id not in episode.observation_ids
        ):
            raise ValueError("membership must be reflected by the episode snapshot")
        with self._transaction(immediate=True) as cursor:
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT night_episode_id
                    FROM sleep_domain_episode_observation_memberships
                    WHERE namespace_id = ? AND data_mode = ?
                      AND observation_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    membership.observation_id,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != membership.night_episode_id:
                    raise ImmutableRecordConflictError(
                        "observation is already assigned to another NightEpisode"
                    )
                return False
            self._write_night_episode(
                cursor,
                namespace,
                episode,
                expected_cas_version=expected_episode_cas,
            )
            self._insert_episode_observation_membership(
                cursor,
                namespace,
                membership,
            )
            if pending_association is not None:
                self._resolve_pending_association(
                    cursor,
                    namespace,
                    pending_association,
                )
        return True

    def commit_night_revision(
        self,
        namespace: DomainNamespace,
        *,
        publication_id: str,
        trigger_id: str,
        revision: NightEpisodeRevision,
        episode: NightEpisode,
        expected_episode_cas: int,
        expected_current_revision_id: str | None,
        event: DomainEvent,
        transition_receipt: LifecycleTransitionReceipt | None = None,
        membership: EpisodeObservationMembership | None = None,
        source_report: SourceReportVersion | None = None,
        pending_association: PendingEpisodeAssociation | None = None,
    ) -> NightRevisionCommitResult:
        """Append a revision, CAS the current pointer and emit its event atomically."""

        self._require_mode(namespace, revision.data_mode)
        self._require_mode(namespace, episode.data_mode)
        self._require_mode(namespace, event.data_mode)
        if transition_receipt is not None:
            self._require_mode(namespace, transition_receipt.data_mode)
        if episode.night_episode_id != revision.night_episode_id:
            raise ValueError("revision and episode ids do not match")
        if episode.current_night_episode_revision_id != (
            revision.night_episode_revision_id
        ):
            raise ValueError("episode current pointer must reference the new revision")
        if revision.night_episode_revision_id not in (
            episode.night_episode_revision_ids
        ):
            raise ValueError("episode must retain the new revision id")
        if membership is not None and (
            membership.night_episode_id != episode.night_episode_id
            or membership.observation_id not in episode.observation_ids
        ):
            raise ValueError("revision membership does not match episode")
        if source_report is not None and (
            source_report.source_report_version_id
            not in episode.source_report_references
        ):
            raise ValueError("revision source report is not linked by episode")
        if pending_association is not None and pending_association.status.value != (
            "associated"
        ):
            raise ValueError("resolved pending association must be associated")

        with self._transaction(immediate=True) as cursor:
            prior = cursor.execute(
                self._sql(
                    """
                    SELECT night_episode_revision_id
                    FROM sleep_domain_revision_publications
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_id = ? AND trigger_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    episode.night_episode_id,
                    trigger_id,
                ),
            ).fetchone()
            if prior is not None:
                stored_revision = cursor.execute(
                    self._sql(
                        """
                        SELECT revision_json
                        FROM sleep_domain_night_episode_revisions
                        WHERE night_episode_revision_id = ? AND namespace_id = ?
                          AND data_mode = ?
                        """
                    ),
                    (
                        str(prior[0]),
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                stored_episode = cursor.execute(
                    self._sql(
                        """
                        SELECT episode_json
                        FROM sleep_domain_night_episodes
                        WHERE night_episode_id = ? AND namespace_id = ?
                          AND data_mode = ?
                        """
                    ),
                    (
                        episode.night_episode_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                stored_event = cursor.execute(
                    self._sql(
                        """
                        SELECT event_json
                        FROM sleep_domain_domain_outbox
                        WHERE event_id = ? AND namespace_id = ? AND data_mode = ?
                        """
                    ),
                    (
                        event.event_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                    ),
                ).fetchone()
                if (
                    stored_revision is None
                    or stored_episode is None
                    or stored_event is None
                ):
                    raise SleepDomainPersistenceError(
                        "revision publication is missing committed resources"
                    )
                return NightRevisionCommitResult(
                    revision=NightEpisodeRevision.model_validate_json(
                        _database_json_text(stored_revision[0])
                    ),
                    episode=NightEpisode.model_validate_json(
                        _database_json_text(stored_episode[0])
                    ),
                    event=DomainEvent.model_validate_json(
                        _database_json_text(stored_event[0])
                    ),
                    created=False,
                )

            lock_suffix = " FOR UPDATE" if self.dialect == "postgres" else ""
            row = cursor.execute(
                self._sql(
                    """
                    SELECT subject_id, night_key, current_revision_id,
                           current_revision_number, cas_version
                    FROM sleep_domain_night_episodes
                    WHERE night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                    + lock_suffix
                ),
                (
                    episode.night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"NightEpisode not found: {episode.night_episode_id}"
                )
            stored_current = None if row[2] is None else str(row[2])
            stored_number = 0 if row[3] is None else int(row[3])
            if (
                int(row[4]) != expected_episode_cas
                or stored_current != expected_current_revision_id
            ):
                raise CasConflictError("NightEpisode revision CAS conflict")
            if str(row[0]) != episode.subject_id or str(row[1]) != episode.night_key:
                raise ImmutableRecordConflictError(
                    "NightEpisode identity cannot change during publication"
                )
            if revision.revision_number != stored_number + 1:
                raise CasConflictError(
                    "revision number must monotonically follow current pointer"
                )
            if revision.parent_revision_id != stored_current:
                raise CasConflictError(
                    "revision parent must be the current revision"
                )

            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_night_episode_revisions (
                      night_episode_revision_id, namespace_id, data_mode,
                      night_episode_id, subject_id, revision_number,
                      parent_revision_id, revision_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    revision.night_episode_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    revision.night_episode_id,
                    revision.subject_id,
                    revision.revision_number,
                    revision.parent_revision_id,
                    revision.model_dump_json(),
                    _dump_datetime(revision.created_at),
                ),
            )
            if membership is not None:
                self._insert_episode_observation_membership(
                    cursor,
                    namespace,
                    membership,
                )
            if source_report is not None:
                self._insert_episode_source_report(
                    cursor,
                    namespace,
                    episode_id=episode.night_episode_id,
                    source_report=source_report,
                    linked_at=revision.created_at,
                )
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_night_episodes
                    SET state = ?, current_revision_id = ?,
                        current_revision_number = ?, cas_version = ?,
                        episode_json = ?, updated_at = ?
                    WHERE night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                    """
                ),
                (
                    episode.state.value,
                    revision.night_episode_revision_id,
                    revision.revision_number,
                    expected_episode_cas + 1,
                    episode.model_dump_json(),
                    _dump_datetime(episode.updated_at),
                    episode.night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    expected_episode_cas,
                ),
            ).rowcount
            if changed != 1:
                raise CasConflictError("NightEpisode revision CAS conflict")
            if transition_receipt is not None:
                self._insert_lifecycle_transition_receipt(
                    cursor,
                    namespace,
                    transition_receipt,
                )
            stored_event = self._insert_domain_event(cursor, namespace, event)
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_revision_publications (
                      publication_id, namespace_id, data_mode,
                      night_episode_id, night_episode_revision_id,
                      trigger_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    publication_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    episode.night_episode_id,
                    revision.night_episode_revision_id,
                    trigger_id,
                    _dump_datetime(revision.created_at),
                ),
            )
            if pending_association is not None:
                self._resolve_pending_association(
                    cursor,
                    namespace,
                    pending_association,
                )
        return NightRevisionCommitResult(
            revision=revision,
            episode=episode,
            event=stored_event,
            created=True,
        )

    def get_quality_assessment(
        self,
        namespace: DomainNamespace,
        *,
        assessment_id: str,
    ) -> DeterministicQualityAssessment | None:
        row = self._fetchone(
            """
            SELECT assessment_json
            FROM sleep_domain_quality_assessments
            WHERE assessment_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                assessment_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return DeterministicQualityAssessment.model_validate_json(
            _database_json_text(row[0])
        )

    def get_current_quality(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> DeterministicQualityAssessment | None:
        row = self._fetchone(
            """
            SELECT assessment_json
            FROM sleep_domain_current_quality
            WHERE namespace_id = ? AND data_mode = ? AND night_episode_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
            ),
        )
        if row is None:
            return None
        return DeterministicQualityAssessment.model_validate_json(
            _database_json_text(row[0])
        )

    def get_current_risk(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> CurrentRisk | None:
        row = self._fetchone(
            """
            SELECT risk_json
            FROM sleep_domain_current_risk
            WHERE namespace_id = ? AND data_mode = ? AND night_episode_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
            ),
        )
        if row is None:
            return None
        return CurrentRisk.model_validate_json(_database_json_text(row[0]))

    def list_vendor_alert_instances(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        open_only: bool = False,
    ) -> tuple[VendorAlertInstance, ...]:
        sql = """
            SELECT instance_json
            FROM sleep_domain_vendor_alert_instances
            WHERE namespace_id = ? AND data_mode = ? AND night_episode_id = ?
        """
        if open_only:
            sql += " AND is_open = TRUE"
        sql += " ORDER BY opened_at, alert_instance_id"
        rows = self._fetchall(
            sql,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
            ),
        )
        return tuple(
            VendorAlertInstance.model_validate_json(
                _database_json_text(row[0])
            )
            for row in rows
        )

    def get_alert_correlation_receipt(
        self,
        namespace: DomainNamespace,
        *,
        observation_id: str,
    ) -> AlertCorrelationReceipt | None:
        row = self._fetchone(
            """
            SELECT receipt_json
            FROM sleep_domain_alert_correlation_receipts
            WHERE namespace_id = ? AND data_mode = ? AND observation_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                observation_id,
            ),
        )
        if row is None:
            return None
        return AlertCorrelationReceipt.model_validate_json(
            _database_json_text(row[0])
        )

    def get_fast_path_signal_projection(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        signal_type: str,
    ) -> FastPathSignalProjection | None:
        row = self._fetchone(
            """
            SELECT projection_json
            FROM sleep_domain_fast_path_signal_projections
            WHERE namespace_id = ? AND data_mode = ? AND night_episode_id = ?
              AND signal_type = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
                signal_type,
            ),
        )
        if row is None:
            return None
        return FastPathSignalProjection.model_validate_json(
            _database_json_text(row[0])
        )

    def commit_deterministic_fast_path(
        self,
        namespace: DomainNamespace,
        *,
        quality: DeterministicQualityAssessment,
        risk: CurrentRisk,
        alert_instances: tuple[VendorAlertInstance, ...],
        alert_receipts: tuple[AlertCorrelationReceipt, ...],
        signal_projections: tuple[FastPathSignalProjection, ...],
        signal_receipts: tuple[FastPathSignalReceipt, ...],
        events: tuple[DomainEvent, ...],
    ) -> bool:
        """Atomically persist deterministic projections, receipts and outbox."""

        self._require_mode(namespace, quality.data_mode)
        self._require_mode(namespace, risk.data_mode)
        if quality.night_episode_id != risk.night_episode_id:
            raise ValueError("quality and risk must target the same NightEpisode")
        emitted = sum(
            receipt.outcome.value == "emitted"
            for receipt in signal_receipts
        )
        if len(events) != emitted:
            raise ValueError("every emitted fast-path receipt requires one event")
        with self._transaction(immediate=True) as cursor:
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT assessment_json
                    FROM sleep_domain_quality_assessments
                    WHERE assessment_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    quality.assessment_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if existing is not None:
                if not _json_equivalent(
                    existing[0],
                    quality.model_dump_json(),
                ):
                    raise ImmutableRecordConflictError(
                        "deterministic quality assessment is append-only"
                    )
                return False

            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_quality_assessments (
                      assessment_id, namespace_id, data_mode, subject_id,
                      night_episode_id, assessed_at, policy_version,
                      assessment_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    quality.assessment_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    quality.subject_id,
                    quality.night_episode_id,
                    _dump_datetime(quality.assessed_at),
                    quality.policy_version,
                    quality.model_dump_json(),
                ),
            )
            quality_current = cursor.execute(
                self._sql(
                    """
                    SELECT cas_version
                    FROM sleep_domain_current_quality
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    quality.night_episode_id,
                ),
            ).fetchone()
            next_quality_cas = (
                1 if quality_current is None else int(quality_current[0]) + 1
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_current_quality (
                      namespace_id, data_mode, subject_id, night_episode_id,
                      assessment_id, cas_version, assessment_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (namespace_id, data_mode, night_episode_id)
                    DO UPDATE SET
                      subject_id = excluded.subject_id,
                      assessment_id = excluded.assessment_id,
                      cas_version = excluded.cas_version,
                      assessment_json = excluded.assessment_json,
                      updated_at = excluded.updated_at
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    quality.subject_id,
                    quality.night_episode_id,
                    quality.assessment_id,
                    next_quality_cas,
                    quality.model_dump_json(),
                    _dump_datetime(quality.assessed_at),
                ),
            )

            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_risk_assessments (
                      current_risk_id, namespace_id, data_mode, subject_id,
                      night_episode_id, observed_at, policy_version, risk_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    risk.current_risk_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    risk.subject_id,
                    risk.night_episode_id,
                    _dump_datetime(risk.observed_at),
                    risk.policy_version,
                    risk.model_dump_json(),
                ),
            )
            risk_current = cursor.execute(
                self._sql(
                    """
                    SELECT cas_version
                    FROM sleep_domain_current_risk
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    risk.night_episode_id,
                ),
            ).fetchone()
            next_risk_cas = (
                1 if risk_current is None else int(risk_current[0]) + 1
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_current_risk (
                      namespace_id, data_mode, subject_id, night_episode_id,
                      current_risk_id, cas_version, risk_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (namespace_id, data_mode, night_episode_id)
                    DO UPDATE SET
                      subject_id = excluded.subject_id,
                      current_risk_id = excluded.current_risk_id,
                      cas_version = excluded.cas_version,
                      risk_json = excluded.risk_json,
                      updated_at = excluded.updated_at
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    risk.subject_id,
                    risk.night_episode_id,
                    risk.current_risk_id,
                    next_risk_cas,
                    risk.model_dump_json(),
                    _dump_datetime(risk.updated_at),
                ),
            )

            for instance in alert_instances:
                self._write_vendor_alert_instance(
                    cursor,
                    namespace,
                    instance,
                )
            for receipt in alert_receipts:
                self._insert_alert_correlation_receipt(
                    cursor,
                    namespace,
                    receipt,
                )
            for projection in signal_projections:
                self._write_fast_path_signal_projection(
                    cursor,
                    namespace,
                    projection,
                )
            for receipt in signal_receipts:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_fast_path_signal_receipts (
                          receipt_id, namespace_id, data_mode, subject_id,
                          night_episode_id, signal_type, evaluation_id,
                          outcome, receipt_json, observed_at, persisted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        receipt.receipt_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        receipt.subject_id,
                        receipt.night_episode_id,
                        receipt.signal_type.value,
                        receipt.evaluation_id,
                        receipt.outcome.value,
                        receipt.model_dump_json(),
                        _dump_datetime(receipt.observed_at),
                        _dump_datetime(receipt.persisted_at),
                    ),
                )
            for event in events:
                self._insert_domain_event(cursor, namespace, event)
        return True

    def get_care_followup(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> CareFollowupSnapshot | None:
        row = self._fetchone(
            """
            SELECT snapshot_json
            FROM sleep_domain_care_followups
            WHERE namespace_id = ? AND data_mode = ? AND night_episode_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
            ),
        )
        if row is None:
            return None
        return CareFollowupSnapshot.model_validate_json(
            _database_json_text(row[0])
        )

    def list_care_followups(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
    ) -> tuple[CareFollowupSnapshot, ...]:
        rows = self._fetchall(
            """
            SELECT snapshot_json
            FROM sleep_domain_care_followups
            WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
            ORDER BY updated_at, night_episode_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                subject_id,
            ),
        )
        return tuple(
            CareFollowupSnapshot.model_validate_json(
                _database_json_text(row[0])
            )
            for row in rows
        )

    def get_care_followup_transition_receipt(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        command_id: str,
    ) -> CareFollowupTransitionReceipt | None:
        row = self._fetchone(
            """
            SELECT receipt_json
            FROM sleep_domain_care_followup_transition_receipts
            WHERE namespace_id = ? AND data_mode = ?
              AND night_episode_id = ? AND command_id = ?
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
                command_id,
            ),
        )
        if row is None:
            return None
        return CareFollowupTransitionReceipt.model_validate_json(
            _database_json_text(row[0])
        )

    def commit_care_followup_transition(
        self,
        namespace: DomainNamespace,
        *,
        snapshot: CareFollowupSnapshot,
        expected_cas_version: int,
        receipt: CareFollowupTransitionReceipt,
        event: DomainEvent,
    ) -> bool:
        self._require_mode(namespace, snapshot.data_mode)
        self._require_mode(namespace, receipt.data_mode)
        self._require_mode(namespace, event.data_mode)
        with self._transaction(immediate=True) as cursor:
            duplicate = cursor.execute(
                self._sql(
                    """
                    SELECT receipt_json
                    FROM sleep_domain_care_followup_transition_receipts
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_id = ? AND command_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    snapshot.night_episode_id,
                    receipt.command_id,
                ),
            ).fetchone()
            if duplicate is not None:
                if not _json_equivalent(
                    duplicate[0],
                    receipt.model_dump_json(),
                ):
                    raise ImmutableRecordConflictError(
                        "care follow-up command id has different content"
                    )
                return False
            row = cursor.execute(
                self._sql(
                    """
                    SELECT cas_version
                    FROM sleep_domain_care_followups
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_id = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    snapshot.night_episode_id,
                ),
            ).fetchone()
            stored_cas = 0 if row is None else int(row[0])
            if stored_cas != expected_cas_version:
                raise CasConflictError("care follow-up CAS conflict")
            if snapshot.cas_version != expected_cas_version + 1:
                raise CasConflictError("care follow-up CAS must increment by one")
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_care_followups (
                      namespace_id, data_mode, subject_id, night_episode_id,
                      state, cas_version, snapshot_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (namespace_id, data_mode, night_episode_id)
                    DO UPDATE SET
                      state = excluded.state,
                      cas_version = excluded.cas_version,
                      snapshot_json = excluded.snapshot_json,
                      updated_at = excluded.updated_at
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    snapshot.subject_id,
                    snapshot.night_episode_id,
                    snapshot.state.value,
                    snapshot.cas_version,
                    snapshot.model_dump_json(),
                    _dump_datetime(snapshot.state_entered_at),
                    _dump_datetime(snapshot.updated_at),
                ),
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_care_followup_transition_receipts (
                      receipt_id, namespace_id, data_mode, subject_id,
                      night_episode_id, command_id, from_state, to_state,
                      actor_id, authorization_id, receipt_json, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    receipt.receipt_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    receipt.subject_id,
                    receipt.night_episode_id,
                    receipt.command_id,
                    receipt.from_state.value,
                    receipt.to_state.value,
                    receipt.actor_id,
                    receipt.authorization_id,
                    receipt.model_dump_json(),
                    _dump_datetime(receipt.occurred_at),
                ),
            )
            self._insert_domain_event(cursor, namespace, event)
        return True

    def get_operation(
        self,
        namespace: DomainNamespace,
        *,
        operation_id: str,
    ) -> Operation | None:
        row = self._fetchone(
            """
            SELECT operation_json
            FROM sleep_domain_operations
            WHERE operation_id = ? AND namespace_id = ? AND data_mode = ?
            """,
            (
                operation_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        if row is None:
            return None
        return Operation.model_validate_json(_database_json_text(row[0]))

    def create_operation(
        self,
        namespace: DomainNamespace,
        operation: Operation,
    ) -> tuple[Operation, bool]:
        self._require_mode(namespace, operation.data_mode)
        operation_json = operation.model_dump_json()
        target_key = operation.target_resource_id or ""
        with self._transaction(immediate=True) as cursor:
            existing = cursor.execute(
                self._sql(
                    """
                    SELECT request_sha256, operation_json
                    FROM sleep_domain_operations
                    WHERE namespace_id = ? AND data_mode = ?
                      AND service_principal_id = ? AND actor_id = ?
                      AND operation_type = ? AND target_resource_key = ?
                      AND idempotency_key = ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    operation.service_principal_id,
                    operation.actor_id,
                    operation.operation_type,
                    target_key,
                    operation.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != operation.request_sha256:
                    raise IdempotencyConflictError(
                        "operation idempotency key has a different request hash"
                    )
                return (
                    Operation.model_validate_json(_database_json_text(existing[1])),
                    False,
                )
            inserted = cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_operations (
                      operation_id, namespace_id, data_mode, operation_type,
                      subject_id, service_principal_id, actor_id,
                      target_resource_id, target_resource_key, idempotency_key,
                      request_sha256, status, attempt_count, lease_owner,
                      lease_expires_at, cas_version, operation_json,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                      namespace_id, data_mode, service_principal_id, actor_id,
                      operation_type, target_resource_key, idempotency_key
                    ) DO NOTHING
                    """
                ),
                (
                    operation.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    operation.operation_type,
                    operation.subject_id,
                    operation.service_principal_id,
                    operation.actor_id,
                    operation.target_resource_id,
                    target_key,
                    operation.idempotency_key,
                    operation.request_sha256,
                    operation.status.value,
                    operation.attempt_count,
                    operation.lease_owner,
                    _dump_optional_datetime(operation.lease_expires_at),
                    0,
                    operation_json,
                    _dump_datetime(operation.created_at),
                    _dump_datetime(operation.updated_at),
                ),
            ).rowcount
            if inserted != 1:
                concurrent = cursor.execute(
                    self._sql(
                        """
                        SELECT request_sha256, operation_json
                        FROM sleep_domain_operations
                        WHERE namespace_id = ? AND data_mode = ?
                          AND service_principal_id = ? AND actor_id = ?
                          AND operation_type = ? AND target_resource_key = ?
                          AND idempotency_key = ?
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        operation.service_principal_id,
                        operation.actor_id,
                        operation.operation_type,
                        target_key,
                        operation.idempotency_key,
                    ),
                ).fetchone()
                if concurrent is None:
                    raise SleepDomainPersistenceError(
                        "operation idempotency conflict was not readable"
                    )
                if str(concurrent[0]) != operation.request_sha256:
                    raise IdempotencyConflictError(
                        "operation idempotency key has a different request hash"
                    )
                return (
                    Operation.model_validate_json(
                        _database_json_text(concurrent[1])
                    ),
                    False,
                )
        return operation, True

    def lease_next_operation(
        self,
        namespace: DomainNamespace,
        *,
        operation_type_prefix: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> LeasedOperation | None:
        """Lease one fair, per-subject-ordered persistent operation.

        Only the head eligible item for a subject can run, and subjects are
        ordered by their last persisted lease time before creation time.  This
        keeps a reanalysis storm for one subject from starving other subjects.
        """

        if not operation_type_prefix:
            raise ValueError("operation_type_prefix is required")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        now_text = _dump_datetime(now)
        lease_expires_at = now + lease_duration
        like_pattern = operation_type_prefix + "%"
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    WITH eligible AS (
                      SELECT operation_id, subject_id, created_at, cas_version,
                             operation_json,
                             ROW_NUMBER() OVER (
                               PARTITION BY subject_id
                               ORDER BY created_at, operation_id
                             ) AS subject_position
                      FROM sleep_domain_operations candidate
                      WHERE namespace_id = ? AND data_mode = ?
                        AND operation_type LIKE ?
                        AND (
                          status = 'pending'
                          OR (
                            status = 'running'
                            AND lease_expires_at <= ?
                          )
                        )
                        AND NOT EXISTS (
                          SELECT 1
                          FROM sleep_domain_operations active
                          WHERE active.namespace_id = candidate.namespace_id
                            AND active.data_mode = candidate.data_mode
                            AND active.operation_type LIKE ?
                            AND active.subject_id = candidate.subject_id
                            AND active.operation_id <> candidate.operation_id
                            AND active.status = 'running'
                            AND active.lease_expires_at > ?
                        )
                    ),
                    last_lease AS (
                      SELECT subject_id, MAX(updated_at) AS last_leased_at
                      FROM sleep_domain_operations
                      WHERE namespace_id = ? AND data_mode = ?
                        AND operation_type LIKE ? AND attempt_count > 0
                      GROUP BY subject_id
                    )
                    SELECT eligible.operation_id, eligible.cas_version,
                           eligible.operation_json
                    FROM eligible
                    LEFT JOIN last_lease
                      ON last_lease.subject_id = eligible.subject_id
                    WHERE eligible.subject_position = 1
                    ORDER BY
                      CASE WHEN last_lease.last_leased_at IS NULL THEN 0 ELSE 1 END,
                      last_lease.last_leased_at,
                      eligible.created_at,
                      eligible.operation_id
                    LIMIT 1
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    like_pattern,
                    now_text,
                    like_pattern,
                    now_text,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    like_pattern,
                ),
            ).fetchone()
            if row is None:
                return None
            existing = Operation.model_validate_json(
                _database_json_text(row[2])
            )
            expected_cas = int(row[1])
            updated = Operation.model_validate(
                {
                    **existing.model_dump(mode="python"),
                    "status": OperationStatus.RUNNING,
                    "attempt_count": existing.attempt_count + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_operations
                    SET status = ?, attempt_count = ?, lease_owner = ?,
                        lease_expires_at = ?, cas_version = ?,
                        operation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                      AND (
                        status = 'pending'
                        OR (status = 'running' AND lease_expires_at <= ?)
                      )
                    """
                ),
                (
                    updated.status.value,
                    updated.attempt_count,
                    worker_id,
                    _dump_datetime(lease_expires_at),
                    expected_cas + 1,
                    updated.model_dump_json(),
                    now_text,
                    updated.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    expected_cas,
                    now_text,
                ),
            ).rowcount
            if changed != 1:
                return None
            return LeasedOperation(
                operation=updated,
                cas_version=expected_cas + 1,
            )

    def lease_operation(
        self,
        namespace: DomainNamespace,
        *,
        operation_id: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        expected_cas_version: int,
    ) -> Operation | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT status, lease_expires_at, cas_version, operation_json
                    FROM sleep_domain_operations
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if row is None or int(row[2]) != expected_cas_version:
                return None
            status = str(row[0])
            if status == OperationStatus.RUNNING.value:
                if row[1] is None or _parse_datetime(str(row[1])) > now:
                    return None
            elif status != OperationStatus.PENDING.value:
                return None
            existing = Operation.model_validate_json(_database_json_text(row[3]))
            lease_expires_at = now + lease_duration
            updated = Operation.model_validate(
                {
                    **existing.model_dump(mode="python"),
                    "status": OperationStatus.RUNNING,
                    "attempt_count": existing.attempt_count + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_operations
                    SET status = ?, attempt_count = ?, lease_owner = ?,
                        lease_expires_at = ?, cas_version = ?,
                        operation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                    """
                ),
                (
                    updated.status.value,
                    updated.attempt_count,
                    updated.lease_owner,
                    _dump_datetime(lease_expires_at),
                    expected_cas_version + 1,
                    updated.model_dump_json(),
                    _dump_datetime(now),
                    operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    expected_cas_version,
                ),
            ).rowcount
            return updated if changed == 1 else None

    def compare_and_set_operation(
        self,
        namespace: DomainNamespace,
        operation: Operation,
        *,
        expected_cas_version: int,
    ) -> bool:
        self._require_mode(namespace, operation.data_mode)
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT cas_version, operation_json
                    FROM sleep_domain_operations
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    operation.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if row is None or int(row[0]) != expected_cas_version:
                return False
            existing = Operation.model_validate_json(
                _database_json_text(row[1])
            )
            if _operation_identity(existing) != _operation_identity(operation):
                raise ImmutableRecordConflictError(
                    "operation request identity is immutable"
                )
            if operation.attempt_count < existing.attempt_count:
                raise ValueError("operation attempt_count cannot decrease")
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_operations
                    SET status = ?, attempt_count = ?, lease_owner = ?,
                        lease_expires_at = ?, cas_version = ?,
                        operation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                    """
                ),
                (
                    operation.status.value,
                    operation.attempt_count,
                    operation.lease_owner,
                    _dump_optional_datetime(operation.lease_expires_at),
                    expected_cas_version + 1,
                    operation.model_dump_json(),
                    _dump_datetime(operation.updated_at),
                    operation.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    expected_cas_version,
                ),
            ).rowcount
            return changed == 1

    def commit_agent_analysis(
        self,
        namespace: DomainNamespace,
        *,
        analysis: AnalysisRevision,
        role_views: tuple[AnalysisRoleView, ...],
        terminal_operation: Operation,
        expected_operation_cas_version: int,
        worker_id: str,
        event: DomainEvent,
        committed_at: datetime,
    ) -> DomainEvent:
        """Atomically append analysis/views/outbox and finish the leased work."""

        self._require_mode(namespace, analysis.data_mode)
        self._require_mode(namespace, terminal_operation.data_mode)
        self._require_mode(namespace, event.data_mode)
        if terminal_operation.status not in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
        }:
            raise ValueError("analysis operation must finish succeeded or failed")
        if terminal_operation.result_resource_id != analysis.analysis_revision_id:
            raise ValueError("operation result must reference the AnalysisRevision")
        if terminal_operation.target_resource_id != analysis.night_episode_revision_id:
            raise ValueError("operation target must be the exact NightEpisode revision")
        if terminal_operation.subject_id != analysis.subject_id:
            raise NamespaceMismatchError("operation and analysis subjects differ")
        if event.operation_id != terminal_operation.operation_id:
            raise ValueError("analysis event must reference its operation")
        if (
            event.aggregate_id != analysis.analysis_revision_id
            or event.night_episode_id != analysis.night_episode_id
            or event.night_episode_revision_id
            != analysis.night_episode_revision_id
            or event.subject_id != analysis.subject_id
        ):
            raise ValueError("analysis event is not bound to the analysis revision")
        expected_roles = set(AnalysisRole)
        actual_roles = {view.role for view in role_views}
        if actual_roles != expected_roles or len(role_views) != len(expected_roles):
            raise ValueError("analysis requires exactly one elder/family/doctor view")
        for view in role_views:
            self._require_mode(namespace, view.data_mode)
            if (
                view.analysis_revision_id != analysis.analysis_revision_id
                or view.night_episode_id != analysis.night_episode_id
                or view.night_episode_revision_id
                != analysis.night_episode_revision_id
                or view.subject_id != analysis.subject_id
            ):
                raise ValueError("role view is not bound to the exact analysis")

        analysis_json = analysis.model_dump_json()
        with self._transaction(immediate=True) as cursor:
            operation_row = cursor.execute(
                self._sql(
                    """
                    SELECT cas_version, status, lease_owner, lease_expires_at,
                           operation_json
                    FROM sleep_domain_operations
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    terminal_operation.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if (
                operation_row is None
                or int(operation_row[0]) != expected_operation_cas_version
                or str(operation_row[1]) != OperationStatus.RUNNING.value
                or str(operation_row[2]) != worker_id
                or operation_row[3] is None
                or _parse_datetime(str(operation_row[3])) < committed_at
            ):
                raise LeaseConflictError("Agent operation lease is not active")
            leased_operation = Operation.model_validate_json(
                _database_json_text(operation_row[4])
            )
            if _operation_identity(leased_operation) != _operation_identity(
                terminal_operation
            ):
                raise ImmutableRecordConflictError(
                    "operation request identity is immutable"
                )
            parent = cursor.execute(
                self._sql(
                    """
                    SELECT subject_id, revision_number
                    FROM sleep_domain_night_episode_revisions
                    WHERE night_episode_revision_id = ?
                      AND night_episode_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    analysis.night_episode_revision_id,
                    analysis.night_episode_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if (
                parent is None
                or str(parent[0]) != analysis.subject_id
                or int(parent[1]) != analysis.night_episode_revision_number
            ):
                raise ValueError(
                    "analysis must reference one exact matching NightEpisode revision"
                )
            prior = cursor.execute(
                self._sql(
                    """
                    SELECT analysis_revision_id, revision_number
                    FROM sleep_domain_analysis_revisions
                    WHERE namespace_id = ? AND data_mode = ?
                      AND night_episode_revision_id = ?
                    ORDER BY revision_number DESC
                    LIMIT 1
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    analysis.night_episode_revision_id,
                ),
            ).fetchone()
            expected_number = 1 if prior is None else int(prior[1]) + 1
            expected_parent = None if prior is None else str(prior[0])
            if (
                analysis.revision_number != expected_number
                or analysis.parent_analysis_revision_id != expected_parent
            ):
                raise CasConflictError("analysis append parent/revision conflict")
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_analysis_revisions (
                      analysis_revision_id, namespace_id, data_mode,
                      night_episode_id, night_episode_revision_id, subject_id,
                      revision_number, parent_analysis_revision_id,
                      analysis_run_id, analysis_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    analysis.analysis_revision_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    analysis.night_episode_id,
                    analysis.night_episode_revision_id,
                    analysis.subject_id,
                    analysis.revision_number,
                    analysis.parent_analysis_revision_id,
                    analysis.analysis_run_id,
                    analysis_json,
                    _dump_datetime(analysis.created_at),
                ),
            )
            for view in role_views:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO sleep_domain_analysis_role_views (
                          role_view_id, namespace_id, data_mode,
                          analysis_revision_id, night_episode_id,
                          night_episode_revision_id, subject_id, role, status,
                          product_agent_episode_id, view_json, generated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        view.role_view_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        view.analysis_revision_id,
                        view.night_episode_id,
                        view.night_episode_revision_id,
                        view.subject_id,
                        view.role.value,
                        view.status.value,
                        view.product_agent_episode_id,
                        view.model_dump_json(),
                        _dump_datetime(view.generated_at),
                    ),
                )
            stored_event = self._insert_domain_event(cursor, namespace, event)
            changed = cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_domain_operations
                    SET status = ?, attempt_count = ?, lease_owner = NULL,
                        lease_expires_at = NULL, cas_version = ?,
                        operation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ? AND cas_version = ?
                      AND status = 'running' AND lease_owner = ?
                    """
                ),
                (
                    terminal_operation.status.value,
                    terminal_operation.attempt_count,
                    expected_operation_cas_version + 1,
                    terminal_operation.model_dump_json(),
                    _dump_datetime(terminal_operation.updated_at),
                    terminal_operation.operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    expected_operation_cas_version,
                    worker_id,
                ),
            ).rowcount
            if changed != 1:
                raise LeaseConflictError("Agent operation lease changed")
        return stored_event

    def lease_processing_outbox(
        self,
        namespace: DomainNamespace,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxLease | None:
        return self._lease_outbox(
            namespace,
            table="sleep_domain_processing_outbox",
            id_column="intent_id",
            json_column="intent_json",
            outbox_kind="processing",
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
        )

    def lease_domain_outbox(
        self,
        namespace: DomainNamespace,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxLease | None:
        return self._lease_outbox(
            namespace,
            table="sleep_domain_domain_outbox",
            id_column="event_id",
            json_column="event_json",
            outbox_kind="domain",
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
        )

    def mark_outbox_delivered(
        self,
        namespace: DomainNamespace,
        lease: OutboxLease,
        *,
        delivered_at: datetime,
    ) -> bool:
        table, id_column = {
            "processing": (
                "sleep_domain_processing_outbox",
                "intent_id",
            ),
            "domain": ("sleep_domain_domain_outbox", "event_id"),
        }[lease.outbox_kind]
        with self._transaction(immediate=True) as cursor:
            changed = cursor.execute(
                self._sql(
                    f"""
                    UPDATE {table}
                    SET status = 'delivered', delivered_at = ?,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE {id_column} = ? AND namespace_id = ?
                      AND data_mode = ? AND status = 'leased'
                      AND lease_owner = ? AND attempt_count = ?
                    """
                ),
                (
                    _dump_datetime(delivered_at),
                    lease.item_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    lease.lease_owner,
                    lease.attempt_count,
                ),
            ).rowcount
            return changed == 1

    def list_episode_source_reports(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> tuple[SourceReportVersion, ...]:
        rows = self._fetchall(
            """
            SELECT report.source_report_version_id, report.provider_id,
                   report.provider_account_id, report.provider_device_key,
                   report.local_report_date, report.report_version,
                   report.content_sha256, report.raw_ingress_record_id,
                   report.is_empty, report.fetched_at
            FROM sleep_domain_episode_source_reports AS link
            JOIN sleep_domain_source_reports AS report
              ON report.source_report_version_id =
                 link.source_report_version_id
            WHERE link.namespace_id = ? AND link.data_mode = ?
              AND link.night_episode_id = ?
            ORDER BY report.report_version, report.fetched_at,
                     report.source_report_version_id
            """,
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                night_episode_id,
            ),
        )
        return tuple(
            SourceReportVersion(
                source_report_version_id=str(row[0]),
                provider_id=str(row[1]),
                provider_account_id=str(row[2]),
                provider_device_key=str(row[3]),
                local_report_date=date.fromisoformat(str(row[4])),
                report_version=int(row[5]),
                content_sha256=str(row[6]),
                raw_ingress_record_id=str(row[7]),
                is_empty=bool(row[8]),
                fetched_at=_parse_datetime(str(row[9])),
                created=False,
            )
            for row in rows
        )

    def count_rows(self, table: str) -> int:
        allowed = {
            "sleep_domain_adapters",
            "sleep_domain_provider_accounts",
            "sleep_domain_device_identities",
            "sleep_domain_device_bindings",
            "sleep_domain_device_binding_audit",
            "sleep_domain_raw_inbox",
            "sleep_domain_processing_receipts",
            "sleep_domain_quarantine",
            "sleep_domain_quarantine_reprocess_audit",
            "sleep_domain_ingress_nonces",
            "sleep_domain_source_reports",
            "sleep_domain_pull_checkpoints",
            "sleep_domain_observation_fact_values",
            "sleep_domain_observation_acquisitions",
            "sleep_domain_observation_conflicts",
            "sleep_domain_adapter_candidates",
            "sleep_domain_canonical_observations",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_analysis_revisions",
            "sleep_domain_operations",
            "sleep_domain_normalization_work",
            "sleep_domain_processing_outbox",
            "sleep_domain_domain_outbox",
            "sleep_domain_adapter_deployment_events",
            "sleep_domain_capability_verification_receipts",
            "sleep_domain_adapter_resolution_locks",
            "sleep_domain_monitoring_snapshots",
            "sleep_domain_subject_lifecycle_leases",
            "sleep_domain_lifecycle_transition_receipts",
            "sleep_domain_episode_observation_memberships",
            "sleep_domain_episode_source_reports",
            "sleep_domain_pending_episode_associations",
            "sleep_domain_revision_publications",
            "sleep_domain_quality_assessments",
            "sleep_domain_current_quality",
            "sleep_domain_risk_assessments",
            "sleep_domain_current_risk",
            "sleep_domain_vendor_alert_instances",
            "sleep_domain_alert_correlation_receipts",
            "sleep_domain_fast_path_signal_projections",
            "sleep_domain_fast_path_signal_receipts",
            "sleep_domain_care_followups",
            "sleep_domain_care_followup_transition_receipts",
        }
        if table not in allowed:
            raise ValueError("unsupported sleep-domain table")
        row = self._fetchone(f"SELECT COUNT(*) FROM {table}")
        return int(row[0]) if row is not None else 0

    def _insert_candidate(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        candidate: AdapterObservationCandidate,
        candidate_json: str | None = None,
    ) -> None:
        candidate_json = candidate_json or candidate.model_dump_json()
        existing = cursor.execute(
            self._sql(
                """
                SELECT candidate_json
                FROM sleep_domain_adapter_candidates
                WHERE candidate_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                candidate.candidate_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if existing is not None:
            if not _json_equivalent(existing[0], candidate_json):
                raise ImmutableRecordConflictError(
                    "AdapterObservationCandidate is immutable"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_adapter_candidates (
                  candidate_id, namespace_id, data_mode,
                  raw_ingress_record_id, provider_account_id, source_key,
                  idempotency_key, observation_type, candidate_json,
                  received_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                candidate.candidate_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                candidate.provenance.raw_ingress_record_id,
                candidate.provider_account_id,
                candidate.source_key,
                candidate.idempotency_key,
                candidate.observation_type.value,
                candidate_json,
                _dump_datetime(candidate.received_at),
                _dump_datetime(candidate.received_at),
            ),
        )

    def _insert_observation_acquisition(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        candidate: AdapterObservationCandidate,
        observation_id: str,
        fact_slot_key: str,
        fact_value_sha256: str,
        acquisition_channel: str,
        acquired_at: datetime,
    ) -> None:
        raw_id = candidate.provenance.raw_ingress_record_id
        acquisition_id = "acquisition:" + hashlib.sha256(
            (
                f"{namespace.namespace_id}|{raw_id}|{candidate.candidate_id}|"
                f"{fact_slot_key}|{fact_value_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_observation_acquisitions (
                  acquisition_id, namespace_id, data_mode, fact_slot_key,
                  value_sha256, observation_id, candidate_id,
                  raw_ingress_record_id, acquisition_channel, acquired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                  namespace_id, data_mode, raw_ingress_record_id,
                  candidate_id, fact_slot_key, value_sha256
                ) DO NOTHING
                """
            ),
            (
                acquisition_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                fact_slot_key,
                fact_value_sha256,
                observation_id,
                candidate.candidate_id,
                raw_id,
                acquisition_channel,
                _dump_datetime(acquired_at),
            ),
        )

    def _validate_observation_binding(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        observation: SleepObservation,
    ) -> None:
        row = cursor.execute(
            self._sql(
                """
                SELECT subject_id, device_id, binding_version
                FROM sleep_domain_device_bindings
                WHERE device_binding_id = ? AND namespace_id = ?
                  AND data_mode = ?
                """
            ),
            (
                observation.device_binding_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        expected = (
            observation.subject_id,
            observation.device_id,
            observation.binding_version,
        )
        if row is None or (str(row[0]), str(row[1]), int(row[2])) != expected:
            raise NamespaceMismatchError(
                "canonical observation requires the exact stored binding version"
            )

    def _insert_processing_receipt(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        receipt: ProcessingReceipt,
    ) -> None:
        receipt_json = receipt.model_dump_json()
        existing = cursor.execute(
            self._sql(
                """
                SELECT receipt_json
                FROM sleep_domain_processing_receipts
                WHERE receipt_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                receipt.receipt_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if existing is not None:
            if not _json_equivalent(existing[0], receipt_json):
                raise ImmutableRecordConflictError(
                    "ProcessingReceipt is append-only"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_processing_receipts (
                  receipt_id, namespace_id, data_mode,
                  raw_ingress_record_id, stage, outcome,
                  quarantine_reason, receipt_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                receipt.receipt_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                receipt.raw_ingress_record_id,
                receipt.stage.value,
                receipt.outcome.value,
                (
                    None
                    if receipt.quarantine_reason is None
                    else receipt.quarantine_reason.value
                ),
                receipt_json,
                _dump_datetime(receipt.occurred_at),
            ),
        )

    def _insert_night_episode(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        episode: NightEpisode,
    ) -> None:
        if episode.current_night_episode_revision_id is not None:
            raise ValueError(
                "new NightEpisode current pointer must be set through revision CAS"
            )
        episode_json = episode.model_dump_json()
        existing = cursor.execute(
            self._sql(
                """
                SELECT episode_json
                FROM sleep_domain_night_episodes
                WHERE night_episode_id = ? AND namespace_id = ?
                  AND data_mode = ?
                """
            ),
            (
                episode.night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if existing is not None:
            if not _json_equivalent(existing[0], episode_json):
                raise ImmutableRecordConflictError(
                    "NightEpisode identity/state snapshot conflicts"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_night_episodes (
                  night_episode_id, namespace_id, data_mode, subject_id,
                  night_key, state, current_revision_id,
                  current_revision_number, cas_version, episode_json,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                episode.night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                episode.subject_id,
                episode.night_key,
                episode.state.value,
                episode.current_night_episode_revision_id,
                None,
                0,
                episode_json,
                _dump_datetime(episode.created_at),
                _dump_datetime(episode.updated_at),
            ),
        )

    def _write_vendor_alert_instance(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        instance: VendorAlertInstance,
    ) -> None:
        existing = cursor.execute(
            self._sql(
                """
                SELECT cas_version, instance_json
                FROM sleep_domain_vendor_alert_instances
                WHERE alert_instance_id = ? AND namespace_id = ?
                  AND data_mode = ?
                """
            ),
            (
                instance.alert_instance_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if existing is None:
            if instance.cas_version != 1:
                raise CasConflictError(
                    "new vendor alert instance must start at CAS version 1"
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_vendor_alert_instances (
                      alert_instance_id, namespace_id, data_mode, subject_id,
                      night_episode_id, provider_id, provider_account_id,
                      device_id, vendor_alert_instance_id, alert_code,
                      is_open, cas_version, instance_json, opened_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    instance.alert_instance_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    instance.subject_id,
                    instance.night_episode_id,
                    instance.provider_id,
                    instance.provider_account_id,
                    instance.device_id,
                    instance.vendor_alert_instance_id,
                    instance.alert_code,
                    instance.closed_at is None,
                    instance.cas_version,
                    instance.model_dump_json(),
                    _dump_datetime(instance.opened_at),
                    _dump_optional_datetime(instance.closed_at),
                ),
            )
            return
        if _json_equivalent(existing[1], instance.model_dump_json()):
            return
        if instance.cas_version != int(existing[0]) + 1:
            raise CasConflictError("vendor alert instance CAS conflict")
        changed = cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_vendor_alert_instances
                SET is_open = ?, cas_version = ?, instance_json = ?,
                    closed_at = ?
                WHERE alert_instance_id = ? AND namespace_id = ?
                  AND data_mode = ? AND cas_version = ?
                """
            ),
            (
                instance.closed_at is None,
                instance.cas_version,
                instance.model_dump_json(),
                _dump_optional_datetime(instance.closed_at),
                instance.alert_instance_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                int(existing[0]),
            ),
        ).rowcount
        if changed != 1:
            raise CasConflictError("vendor alert instance CAS conflict")

    def _insert_alert_correlation_receipt(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        receipt: AlertCorrelationReceipt,
    ) -> None:
        existing = cursor.execute(
            self._sql(
                """
                SELECT receipt_json
                FROM sleep_domain_alert_correlation_receipts
                WHERE namespace_id = ? AND data_mode = ? AND observation_id = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                receipt.observation_id,
            ),
        ).fetchone()
        if existing is not None:
            if not _json_equivalent(existing[0], receipt.model_dump_json()):
                raise ImmutableRecordConflictError(
                    "alert correlation receipt is append-only"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_alert_correlation_receipts (
                  receipt_id, namespace_id, data_mode, subject_id,
                  night_episode_id, observation_id, outcome, receipt_json,
                  occurred_at, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                receipt.receipt_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                receipt.subject_id,
                receipt.night_episode_id,
                receipt.observation_id,
                receipt.outcome.value,
                receipt.model_dump_json(),
                _dump_datetime(receipt.occurred_at),
                _dump_datetime(receipt.persisted_at),
            ),
        )

    def _write_fast_path_signal_projection(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        projection: FastPathSignalProjection,
    ) -> None:
        existing = cursor.execute(
            self._sql(
                """
                SELECT cas_version, projection_json
                FROM sleep_domain_fast_path_signal_projections
                WHERE namespace_id = ? AND data_mode = ?
                  AND night_episode_id = ? AND signal_type = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                projection.night_episode_id,
                projection.signal_type.value,
            ),
        ).fetchone()
        if existing is None:
            if projection.cas_version != 1:
                raise CasConflictError(
                    "new fast-path projection must start at CAS version 1"
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_fast_path_signal_projections (
                      namespace_id, data_mode, subject_id, night_episode_id,
                      signal_type, current_state, cas_version,
                      projection_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    projection.subject_id,
                    projection.night_episode_id,
                    projection.signal_type.value,
                    projection.current_state,
                    projection.cas_version,
                    projection.model_dump_json(),
                    _dump_datetime(projection.updated_at),
                ),
            )
            return
        if _json_equivalent(existing[1], projection.model_dump_json()):
            return
        if projection.cas_version != int(existing[0]) + 1:
            raise CasConflictError("fast-path projection CAS conflict")
        changed = cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_fast_path_signal_projections
                SET current_state = ?, cas_version = ?, projection_json = ?,
                    updated_at = ?
                WHERE namespace_id = ? AND data_mode = ?
                  AND night_episode_id = ? AND signal_type = ?
                  AND cas_version = ?
                """
            ),
            (
                projection.current_state,
                projection.cas_version,
                projection.model_dump_json(),
                _dump_datetime(projection.updated_at),
                namespace.namespace_id,
                namespace.data_mode.value,
                projection.night_episode_id,
                projection.signal_type.value,
                int(existing[0]),
            ),
        ).rowcount
        if changed != 1:
            raise CasConflictError("fast-path projection CAS conflict")

    def _write_monitoring_snapshot(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        snapshot: MonitoringSnapshot,
        *,
        expected_cas_version: int | None,
    ) -> None:
        snapshot_json = snapshot.model_dump_json()
        row = cursor.execute(
            self._sql(
                """
                SELECT cas_version, snapshot_json
                FROM sleep_domain_monitoring_snapshots
                WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                snapshot.subject_id,
            ),
        ).fetchone()
        if row is None:
            if expected_cas_version not in (None, 0):
                raise CasConflictError(
                    "new monitoring snapshot expected CAS must be zero"
                )
            if snapshot.cas_version != 1:
                raise CasConflictError(
                    "first committed monitoring snapshot must use CAS version 1"
                )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_domain_monitoring_snapshots (
                      namespace_id, data_mode, subject_id, state,
                      active_night_episode_id, cas_version, snapshot_json,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    snapshot.subject_id,
                    snapshot.state.value,
                    snapshot.active_night_episode_id,
                    snapshot.cas_version,
                    snapshot_json,
                    _dump_datetime(snapshot.state_entered_at),
                    _dump_datetime(snapshot.updated_at),
                ),
            )
            return
        if _json_equivalent(row[1], snapshot_json):
            return
        if expected_cas_version is None or int(row[0]) != expected_cas_version:
            raise CasConflictError("monitoring state CAS conflict")
        if snapshot.cas_version != expected_cas_version + 1:
            raise CasConflictError("monitoring snapshot CAS must increment by one")
        changed = cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_monitoring_snapshots
                SET state = ?, active_night_episode_id = ?, cas_version = ?,
                    snapshot_json = ?, updated_at = ?
                WHERE namespace_id = ? AND data_mode = ? AND subject_id = ?
                  AND cas_version = ?
                """
            ),
            (
                snapshot.state.value,
                snapshot.active_night_episode_id,
                snapshot.cas_version,
                snapshot_json,
                _dump_datetime(snapshot.updated_at),
                namespace.namespace_id,
                namespace.data_mode.value,
                snapshot.subject_id,
                expected_cas_version,
            ),
        ).rowcount
        if changed != 1:
            raise CasConflictError("monitoring state CAS conflict")

    def _insert_lifecycle_transition_receipt(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        receipt: LifecycleTransitionReceipt,
    ) -> None:
        receipt_json = receipt.model_dump_json()
        existing = cursor.execute(
            self._sql(
                """
                SELECT receipt_json
                FROM sleep_domain_lifecycle_transition_receipts
                WHERE namespace_id = ? AND data_mode = ? AND component = ?
                  AND aggregate_id = ? AND trigger_id = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                receipt.component.value,
                receipt.aggregate_id,
                receipt.trigger_id,
            ),
        ).fetchone()
        if existing is not None:
            if not _json_equivalent(existing[0], receipt_json):
                raise ImmutableRecordConflictError(
                    "lifecycle transition receipt is append-only"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_lifecycle_transition_receipts (
                  transition_receipt_id, namespace_id, data_mode, component,
                  aggregate_id, subject_id, trigger_id, from_state, to_state,
                  receipt_json, occurred_at, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                receipt.transition_receipt_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                receipt.component.value,
                receipt.aggregate_id,
                receipt.subject_id,
                receipt.trigger_id,
                receipt.from_state,
                receipt.to_state,
                receipt_json,
                _dump_datetime(receipt.occurred_at),
                _dump_datetime(receipt.committed_at),
            ),
        )

    def _insert_episode_observation_membership(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        membership: EpisodeObservationMembership,
    ) -> None:
        membership_json = _canonical_json_text(
            {
                "membership_id": membership.membership_id,
                "night_episode_id": membership.night_episode_id,
                "observation_id": membership.observation_id,
                "subject_id": membership.subject_id,
                "device_binding_id": membership.device_binding_id,
                "binding_version": membership.binding_version,
                "event_at": membership.event_at.isoformat(),
                "received_at": membership.received_at.isoformat(),
                "lateness_watermark_at": (
                    None
                    if membership.lateness_watermark_at is None
                    else membership.lateness_watermark_at.isoformat()
                ),
                "late_after_watermark": membership.late_after_watermark,
                "associated_at": membership.associated_at.isoformat(),
            }
        )
        existing = cursor.execute(
            self._sql(
                """
                SELECT night_episode_id, membership_json
                FROM sleep_domain_episode_observation_memberships
                WHERE namespace_id = ? AND data_mode = ? AND observation_id = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                membership.observation_id,
            ),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[0]) != membership.night_episode_id
                or not _json_equivalent(existing[1], membership_json)
            ):
                raise ImmutableRecordConflictError(
                    "observation membership is immutable"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_episode_observation_memberships (
                  membership_id, namespace_id, data_mode, night_episode_id,
                  observation_id, subject_id, device_binding_id,
                  binding_version, event_at, received_at,
                  lateness_watermark_at, late_after_watermark,
                  membership_json, associated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                membership.membership_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                membership.night_episode_id,
                membership.observation_id,
                membership.subject_id,
                membership.device_binding_id,
                membership.binding_version,
                _dump_datetime(membership.event_at),
                _dump_datetime(membership.received_at),
                _dump_optional_datetime(membership.lateness_watermark_at),
                membership.late_after_watermark,
                membership_json,
                _dump_datetime(membership.associated_at),
            ),
        )

    def _insert_episode_source_report(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        episode_id: str,
        source_report: SourceReportVersion,
        linked_at: datetime,
    ) -> None:
        link_id = "episode-report:" + hashlib.sha256(
            (
                f"{namespace.namespace_id}|{episode_id}|"
                f"{source_report.source_report_version_id}"
            ).encode("utf-8")
        ).hexdigest()
        existing = cursor.execute(
            self._sql(
                """
                SELECT night_episode_id
                FROM sleep_domain_episode_source_reports
                WHERE namespace_id = ? AND data_mode = ?
                  AND source_report_version_id = ?
                """
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                source_report.source_report_version_id,
            ),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != episode_id:
                raise ImmutableRecordConflictError(
                    "source report is already linked to another NightEpisode"
                )
            return
        cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_episode_source_reports (
                  link_id, namespace_id, data_mode, night_episode_id,
                  source_report_version_id, report_version, content_sha256,
                  linked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                link_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                episode_id,
                source_report.source_report_version_id,
                source_report.report_version,
                source_report.content_sha256,
                _dump_datetime(linked_at),
            ),
        )

    def _resolve_pending_association(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        association: PendingEpisodeAssociation,
    ) -> None:
        changed = cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_pending_episode_associations
                SET status = ?, reason_code = ?, association_json = ?,
                    resolved_at = ?
                WHERE namespace_id = ? AND data_mode = ?
                  AND association_kind = ? AND source_resource_id = ?
                  AND status = 'pending'
                """
            ),
            (
                association.status.value,
                association.reason_code,
                association.model_dump_json(),
                _dump_optional_datetime(association.resolved_at),
                namespace.namespace_id,
                namespace.data_mode.value,
                association.association_kind.value,
                association.source_resource_id,
            ),
        ).rowcount
        if changed not in (0, 1):
            raise SleepDomainPersistenceError(
                "pending association resolution changed multiple rows"
            )

    def _write_night_episode(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        episode: NightEpisode,
        *,
        expected_cas_version: int | None,
    ) -> None:
        episode_json = episode.model_dump_json()
        row = cursor.execute(
            self._sql(
                """
                SELECT subject_id, night_key, current_revision_id,
                       cas_version, episode_json
                FROM sleep_domain_night_episodes
                WHERE night_episode_id = ? AND namespace_id = ?
                  AND data_mode = ?
                """
            ),
            (
                episode.night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if row is None:
            if expected_cas_version not in (None, 0):
                raise CasConflictError("new NightEpisode expected CAS must be zero")
            self._insert_night_episode(cursor, namespace, episode)
            return
        if _json_equivalent(row[4], episode_json):
            return
        if str(row[0]) != episode.subject_id or str(row[1]) != episode.night_key:
            raise ImmutableRecordConflictError(
                "NightEpisode subject and night identity are immutable"
            )
        stored_current = None if row[2] is None else str(row[2])
        if stored_current != episode.current_night_episode_revision_id:
            raise ImmutableRecordConflictError(
                "NightEpisode current pointer can change only through pointer CAS"
            )
        if expected_cas_version is None or int(row[3]) != expected_cas_version:
            raise CasConflictError("NightEpisode state CAS conflict")
        changed = cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_night_episodes
                SET state = ?, cas_version = ?, episode_json = ?, updated_at = ?
                WHERE night_episode_id = ? AND namespace_id = ?
                  AND data_mode = ? AND cas_version = ?
                """
            ),
            (
                episode.state.value,
                expected_cas_version + 1,
                episode_json,
                _dump_datetime(episode.updated_at),
                episode.night_episode_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                expected_cas_version,
            ),
        ).rowcount
        if changed != 1:
            raise CasConflictError("NightEpisode state CAS conflict")

    def _insert_domain_event(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        event: DomainEvent,
    ) -> DomainEvent:
        existing = cursor.execute(
            self._sql(
                """
                SELECT event_json
                FROM sleep_domain_domain_outbox
                WHERE event_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                event.event_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        ).fetchone()
        if existing is not None:
            stored = DomainEvent.model_validate_json(
                _database_json_text(existing[0])
            )
            if stored != event.model_copy(
                update={"delivery_offset": stored.delivery_offset}
            ):
                raise ImmutableRecordConflictError("DomainEvent is immutable")
            return stored
        row = cursor.execute(
            self._sql(
                """
                INSERT INTO sleep_domain_domain_outbox (
                  event_id, namespace_id, data_mode, event_type,
                  aggregate_type, aggregate_id, aggregate_version,
                  per_aggregate_sequence, subject_id, operation_id,
                  status, attempt_count, available_at, lease_owner,
                  lease_expires_at, event_json, created_at, delivered_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                RETURNING delivery_offset
                """
            ),
            (
                event.event_id,
                namespace.namespace_id,
                namespace.data_mode.value,
                event.event_type.value,
                event.aggregate_type,
                event.aggregate_id,
                event.aggregate_version,
                event.per_aggregate_sequence,
                event.subject_id,
                event.operation_id,
                "pending",
                0,
                _dump_datetime(event.persisted_at),
                None,
                None,
                "{}",
                _dump_datetime(event.persisted_at),
                None,
            ),
        ).fetchone()
        if row is None:
            raise SleepDomainPersistenceError(
                "database did not return a domain delivery offset"
            )
        stored = DomainEvent.model_validate(
            {
                **event.model_dump(mode="python"),
                "delivery_offset": int(row[0]),
            }
        )
        cursor.execute(
            self._sql(
                """
                UPDATE sleep_domain_domain_outbox
                SET event_json = ?
                WHERE event_id = ? AND namespace_id = ? AND data_mode = ?
                """
            ),
            (
                stored.model_dump_json(),
                event.event_id,
                namespace.namespace_id,
                namespace.data_mode.value,
            ),
        )
        return stored

    def _lease_outbox(
        self,
        namespace: DomainNamespace,
        *,
        table: str,
        id_column: str,
        json_column: str,
        outbox_kind: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        now_text = _dump_datetime(now)
        lease_expires_at = now + lease_duration
        with self._transaction(immediate=True) as cursor:
            suffix = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
            row = cursor.execute(
                self._sql(
                    f"""
                    SELECT {id_column}, attempt_count, {json_column}
                    FROM {table}
                    WHERE namespace_id = ? AND data_mode = ?
                      AND (
                        (status = 'pending' AND available_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                      )
                    ORDER BY available_at, created_at, {id_column}
                    LIMIT 1
                    """
                    + suffix
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    now_text,
                    now_text,
                ),
            ).fetchone()
            if row is None:
                return None
            next_attempt = int(row[1]) + 1
            changed = cursor.execute(
                self._sql(
                    f"""
                    UPDATE {table}
                    SET status = 'leased', attempt_count = ?,
                        lease_owner = ?, lease_expires_at = ?
                    WHERE {id_column} = ? AND namespace_id = ?
                      AND data_mode = ? AND attempt_count = ?
                      AND (
                        (status = 'pending' AND available_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                      )
                    """
                ),
                (
                    next_attempt,
                    worker_id,
                    _dump_datetime(lease_expires_at),
                    str(row[0]),
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    int(row[1]),
                    now_text,
                    now_text,
                ),
            ).rowcount
            if changed != 1:
                return None
            return OutboxLease(
                outbox_kind=outbox_kind,
                item_id=str(row[0]),
                attempt_count=next_attempt,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                payload_json=_database_json_text(row[2]),
            )

    def _lock_provider_account(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        provider_account_id: str,
        expected_provider_id: str,
    ) -> None:
        suffix = " FOR UPDATE" if self.dialect == "postgres" else ""
        row = cursor.execute(
            self._sql(
                """
                SELECT provider_id
                FROM sleep_domain_provider_accounts
                WHERE namespace_id = ? AND data_mode = ?
                  AND provider_account_id = ?
                """
                + suffix
            ),
            (
                namespace.namespace_id,
                namespace.data_mode.value,
                provider_account_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(f"provider account not found: {provider_account_id}")
        if str(row[0]) != expected_provider_id:
            raise NamespaceMismatchError(
                "provider account provider_id does not match record"
            )

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[Any]:
        with self.store.transaction_lock:
            cursor = self.connection.cursor()
            try:
                if self.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def _fetchone(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> Any | None:
        with self.store.transaction_lock:
            return self.connection.execute(self._sql(sql), params).fetchone()

    def _fetchall(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[Any]:
        with self.store.transaction_lock:
            return list(self.connection.execute(self._sql(sql), params).fetchall())

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    @staticmethod
    def _require_mode(namespace: DomainNamespace, mode: DataMode) -> None:
        if namespace.data_mode != mode:
            raise NamespaceMismatchError(
                "record data_mode does not match repository namespace"
            )


def _dump_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persistence timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _dump_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _dump_datetime(value)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _database_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _canonical_json_text(value: Any) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_equivalent(left: Any, right: Any) -> bool:
    return _canonical_json_text(left) == _canonical_json_text(right)


def _database_datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return _dump_datetime(value)
    return _dump_datetime(_parse_datetime(str(value)))


def _dump_plain_json(value: Mapping[str, JsonScalar]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} is required")


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


def _reject_secret_metadata(metadata: Mapping[str, JsonScalar]) -> None:
    forbidden_fragments = ("secret", "password", "token", "credential", "api_key")
    violations = [
        key
        for key in metadata
        if any(fragment in key.lower() for fragment in forbidden_fragments)
    ]
    if violations:
        raise ValueError(
            "provider account metadata cannot contain secret fields: "
            + ", ".join(sorted(violations))
        )


def _operation_identity(operation: Operation) -> tuple[Any, ...]:
    return (
        operation.operation_id,
        operation.data_mode,
        operation.operation_type,
        operation.subject_id,
        operation.service_principal_id,
        operation.actor_id,
        operation.target_resource_id,
        operation.idempotency_key,
        operation.request_sha256,
        operation.correlation_id,
        operation.created_at,
    )


__all__ = [
    "CandidatePromotionCommitResult",
    "CurrentRevisionPointer",
    "CasConflictError",
    "DeviceIdentityConflictError",
    "DeviceBindingOverlapError",
    "DomainNamespace",
    "IdempotencyConflictError",
    "IngressReplayGuard",
    "ImmutableRecordConflictError",
    "IntakeResult",
    "LeasedOperation",
    "LeaseConflictError",
    "NamespaceMismatchError",
    "NormalizationCommitResult",
    "NormalizationWorkLease",
    "ObservationConflictRecord",
    "OutboxLease",
    "ProviderAccountRecord",
    "QuarantineEntry",
    "ReplayConflictError",
    "SleepDomainPersistenceError",
    "SleepDomainRepository",
]
