"""Audited one-time migration from the deprecated Perceptor webhook SQLite.

The importer is intentionally one-way.  It opens the legacy database read-only,
preserves each old raw id and payload digest, and writes through the unified
encrypted Raw Inbox transaction.  A record is eligible for normalization only
when an immutable manifest proves mode, signature, time and binding; all other
records go to an isolated raw-quarantine namespace.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import ConfigDict, BaseModel, Field, model_validator

from sleepagent.sleep_domain.contracts import (
    DataMode,
    ProcessingOutcome,
    ProcessingReceipt,
    ProcessingStage,
    QuarantineReason,
    RawIngressRecord,
    SignatureVerificationState,
)
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    ProviderAccountRecord,
    SleepDomainRepository,
    TerminalRawQuarantine,
)


UTC = timezone.utc
IMPORTER_VERSION = "legacy-perceptor-webhook-importer.v1"
LEGACY_QUARANTINE_NAMESPACE_MARKER = "legacy-quarantine"


class LegacyImportError(RuntimeError):
    pass


class LegacyImportInProgressError(LegacyImportError):
    pass


class LegacyRecordEvidence(BaseModel):
    """Record-level proof; absence of any required proof causes quarantine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy_raw_event_id: str = Field(..., min_length=1)
    source_data_mode: DataMode | None = None
    signature_verification: SignatureVerificationState = (
        SignatureVerificationState.UNKNOWN
    )
    signature_representation: Literal[
        "canonical_parameters",
        "raw_body",
    ] | None = None
    signature_profile: str | None = None
    signed_payload_sha256: str | None = None
    request_signed_at: datetime | None = None
    event_occurred_at: datetime | None = None
    trustworthy_event_time: bool = False
    device_binding_id: str | None = None
    provider_device_identifier_sha256: str | None = None
    adapter_resolution_lock_id: str | None = None
    compatibility_profile_id: str | None = None
    evidence_reference: str | None = None
    evidence_sha256: str | None = None

    @model_validator(mode="after")
    def evidence_is_coherent(self) -> "LegacyRecordEvidence":
        for value in (self.request_signed_at, self.event_occurred_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("legacy evidence timestamps must be timezone-aware")
        if self.signature_verification == SignatureVerificationState.VERIFIED:
            if (
                not self.signature_profile
                or not self.signature_representation
                or not self.signed_payload_sha256
                or self.request_signed_at is None
                or not self.evidence_reference
                or not self.evidence_sha256
            ):
                raise ValueError(
                    "verified legacy signature requires profile, signed time "
                    "and immutable evidence"
                )
        for name, digest in (
            ("evidence_sha256", self.evidence_sha256),
            ("signed_payload_sha256", self.signed_payload_sha256),
            (
                "provider_device_identifier_sha256",
                self.provider_device_identifier_sha256,
            ),
        ):
            if digest is not None and (
                len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError(f"legacy {name} must be lowercase SHA-256")
        return self


class LegacyImportManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["legacy_import_manifest.v1"] = (
        "legacy_import_manifest.v1"
    )
    records: tuple[LegacyRecordEvidence, ...] = ()

    @model_validator(mode="after")
    def record_ids_are_unique(self) -> "LegacyImportManifest":
        ids = [item.legacy_raw_event_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("legacy manifest record ids must be unique")
        return self

    def sha256(self) -> str:
        return hashlib.sha256(
            self.model_dump_json().encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class LegacyImportReport:
    import_run_id: str
    source_fingerprint_sha256: str
    manifest_sha256: str
    source_record_count: int
    imported_record_count: int
    quarantined_record_count: int
    duplicate_record_count: int
    status: str


@dataclass(frozen=True)
class _LegacyRawRow:
    source_record_key: str
    legacy_raw_event_id: str
    vendor: str
    message_id: str | None
    event_type: str
    device_identifier: str | None
    received_at_text: str
    raw_payload_text: str
    normalization_status: str
    normalized_event_type: str | None
    normalized_payload_sha256: str | None
    raw_payload_available: bool = True


class LegacyWebhookImporter:
    def __init__(
        self,
        *,
        repository: SleepDomainRepository,
        confirmed_namespace: DomainNamespace,
        quarantine_namespace: DomainNamespace,
        provider_account_id: str,
        environment: str,
    ) -> None:
        if (
            quarantine_namespace.data_mode != DataMode.REPLAY
            or LEGACY_QUARANTINE_NAMESPACE_MARKER
            not in quarantine_namespace.namespace_id
        ):
            raise ValueError(
                "unconfirmed legacy records require an isolated "
                "replay:legacy-quarantine namespace"
            )
        if confirmed_namespace == quarantine_namespace:
            raise ValueError("confirmed and quarantine namespaces must differ")
        if not provider_account_id:
            raise ValueError("provider_account_id is required")
        self.repository = repository
        self.store = repository.store
        self.confirmed_namespace = confirmed_namespace
        self.quarantine_namespace = quarantine_namespace
        self.provider_account_id = provider_account_id
        self.environment = environment

    def import_sqlite(
        self,
        source_path: str | Path,
        *,
        manifest: LegacyImportManifest | None = None,
        imported_at: datetime | None = None,
    ) -> LegacyImportReport:
        path = Path(source_path).resolve()
        if not path.is_file():
            raise LegacyImportError("legacy webhook SQLite file does not exist")
        imported_at = imported_at or datetime.now(tz=UTC)
        _require_aware(imported_at, "imported_at")
        manifest = manifest or LegacyImportManifest()
        evidence_by_id = {
            item.legacy_raw_event_id: item for item in manifest.records
        }
        rows = _read_legacy_rows(path)
        source_ids = {row.legacy_raw_event_id for row in rows}
        unknown_manifest_ids = set(evidence_by_id) - source_ids
        if unknown_manifest_ids:
            raise LegacyImportError(
                "legacy manifest references source ids that do not exist"
            )
        source_fingerprint = _source_fingerprint(rows)
        manifest_sha256 = manifest.sha256()
        run_id = _stable_id(
            "legacy-import-run",
            source_fingerprint,
            manifest_sha256,
            self.confirmed_namespace.namespace_id,
            self.quarantine_namespace.namespace_id,
        )
        try:
            existing = self._begin_run(
                run_id=run_id,
                source_fingerprint=source_fingerprint,
                manifest_sha256=manifest_sha256,
                source_record_count=len(rows),
                started_at=imported_at,
            )
        except LegacyImportInProgressError:
            return self._await_run(
                run_id=run_id,
                source_fingerprint=source_fingerprint,
                manifest_sha256=manifest_sha256,
                timeout_seconds=5.0,
            )
        if existing is not None:
            return existing
        try:
            self._require_provider_accounts(imported_at)
            imported = quarantined = duplicates = 0
            for row in rows:
                prior = self._record_disposition(
                    run_id=run_id,
                    source_record_key=row.source_record_key,
                )
                if prior is not None:
                    imported += prior != "duplicate"
                    quarantined += prior == "quarantined"
                    duplicates += prior == "duplicate"
                    continue
                raw_bytes = row.raw_payload_text.encode("utf-8")
                payload_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                evidence = evidence_by_id.get(row.legacy_raw_event_id)
                disposition, target, reason, detail_code = self._classify(
                    row,
                    evidence,
                    payload_sha256=payload_sha256,
                )
                received_at, receipt_time_trustworthy = _legacy_received_at(
                    row.received_at_text,
                    fallback=imported_at,
                )
                if not receipt_time_trustworthy:
                    disposition = "quarantined"
                    target = self.quarantine_namespace
                    reason = QuarantineReason.UNPARSEABLE_TIME
                    detail_code = ",".join(
                        filter(
                            None,
                            (
                                detail_code,
                                "legacy_received_at_unparseable",
                            ),
                        )
                    )
                signature_state = (
                    SignatureVerificationState.UNKNOWN
                    if evidence is None
                    else evidence.signature_verification
                )
                raw_id = row.legacy_raw_event_id
                record = RawIngressRecord(
                    raw_ingress_record_id=raw_id,
                    data_mode=target.data_mode,
                    provider_id="perceptor",
                    provider_account_id=self.provider_account_id,
                    event_type=row.event_type,
                    message_id=row.message_id,
                    request_signed_at=(
                        None if evidence is None else evidence.request_signed_at
                    ),
                    measurement_at=None,
                    event_occurred_at=(
                        None if evidence is None else evidence.event_occurred_at
                    ),
                    received_at=received_at,
                    signature_profile=(
                        None if evidence is None else evidence.signature_profile
                    ),
                    signature_verification=signature_state,
                    idempotency_identity=(
                        f"legacy-webhook.v1:{source_fingerprint}:"
                        f"{row.source_record_key}"
                    ),
                    idempotency_version="legacy-source-row.v1",
                    pre_normalization_payload_sha256=payload_sha256,
                    encrypted_payload_reference=(
                        f"db:sleep_domain_raw_inbox:{raw_id}"
                    ),
                    content_type="application/json",
                    payload_size_bytes=len(raw_bytes),
                    retention_deadline=(
                        received_at
                        + self.repository.raw_payload_policy.retention_period
                    ),
                )
                terminal = None
                if disposition == "quarantined":
                    assert reason is not None and detail_code is not None
                    terminal = TerminalRawQuarantine(
                        quarantine_id=f"quarantine:legacy-import:{raw_id}",
                        receipt=ProcessingReceipt(
                            receipt_id=f"receipt:legacy-import:{raw_id}",
                            raw_ingress_record_id=raw_id,
                            data_mode=target.data_mode,
                            stage=_quarantine_stage(reason),
                            outcome=ProcessingOutcome.QUARANTINED,
                            occurred_at=imported_at,
                            actor_id="legacy-webhook-importer",
                            processor_id=IMPORTER_VERSION,
                            processor_version="1.0.0",
                            quarantine_reason=reason,
                            detail_code=detail_code,
                        ),
                        detail={
                            "source_fingerprint_sha256": source_fingerprint,
                            "legacy_normalization_status": (
                                row.normalization_status
                            ),
                            "legacy_normalized_payload_sha256": (
                                row.normalized_payload_sha256
                            ),
                            "missing_proofs": detail_code,
                            "original_raw_id_preserved": True,
                        },
                    )
                result = self.repository.intake_raw(
                    target,
                    record,
                    raw_payload=raw_bytes,
                    work_id=f"normalize:legacy-import:{raw_id}",
                    work_generation=1,
                    work_json={
                        "adapter_resolution_lock_id": (
                            "quarantined"
                            if evidence is None
                            else (
                                evidence.adapter_resolution_lock_id
                                or "quarantined"
                            )
                        ),
                        "compatibility_profile_id": (
                            "unconfirmed"
                            if evidence is None
                            else (
                                evidence.compatibility_profile_id
                                or "unconfirmed"
                            )
                        ),
                        "environment": self.environment,
                        "ingress_channel": "legacy_webhook_import",
                        "legacy_normalized_event_type": (
                            row.normalized_event_type
                        ),
                        "legacy_normalized_payload_sha256": (
                            row.normalized_payload_sha256
                        ),
                    },
                    processing_intent_id=f"processing-intent:legacy:{raw_id}",
                    processing_intent_json={
                        "event_type": "LEGACY_RAW_IMPORTED",
                        "source_fingerprint_sha256": source_fingerprint,
                    },
                    processing_intent_event_type="LEGACY_RAW_IMPORTED",
                    created_at=imported_at,
                    terminal_quarantine=terminal,
                )
                actual_disposition = (
                    "duplicate"
                    if not result.created
                    else disposition
                )
                self._save_record(
                    run_id=run_id,
                    row=row,
                    target=target,
                    raw_id=result.raw_ingress_record_id,
                    payload_sha256=payload_sha256,
                    disposition=actual_disposition,
                    reason=reason,
                    detail_code=detail_code,
                    imported_at=imported_at,
                )
                imported += result.created
                quarantined += result.created and disposition == "quarantined"
                duplicates += not result.created
            report = LegacyImportReport(
                import_run_id=run_id,
                source_fingerprint_sha256=source_fingerprint,
                manifest_sha256=manifest_sha256,
                source_record_count=len(rows),
                imported_record_count=imported,
                quarantined_record_count=quarantined,
                duplicate_record_count=duplicates,
                status="succeeded",
            )
            self._finish_run(report, completed_at=imported_at)
            return report
        except Exception as exc:
            self._fail_run(
                run_id,
                failure_code=exc.__class__.__name__,
                completed_at=imported_at,
            )
            raise

    def _classify(
        self,
        row: _LegacyRawRow,
        evidence: LegacyRecordEvidence | None,
        *,
        payload_sha256: str,
    ) -> tuple[
        Literal["normalization_pending", "quarantined"],
        DomainNamespace,
        QuarantineReason | None,
        str | None,
    ]:
        missing: list[str] = []
        if row.vendor.strip().lower() != "perceptor":
            missing.append("provider_unconfirmed")
        if not row.raw_payload_available:
            missing.append("original_raw_payload_missing")
        if evidence is None or evidence.source_data_mode is None:
            missing.append("data_mode_unconfirmed")
        elif evidence.source_data_mode != self.confirmed_namespace.data_mode:
            missing.append("data_mode_mismatch")
        if (
            evidence is None
            or evidence.signature_verification
            != SignatureVerificationState.VERIFIED
        ):
            missing.append("signature_unconfirmed")
        elif evidence.signed_payload_sha256 != payload_sha256:
            missing.append("signed_payload_mismatch")
        if (
            evidence is None
            or not evidence.trustworthy_event_time
            or evidence.event_occurred_at is None
        ):
            missing.append("event_time_unconfirmed")
        if (
            evidence is None
            or not evidence.device_binding_id
            or not evidence.adapter_resolution_lock_id
        ):
            missing.append("binding_unconfirmed")
        elif (
            row.device_identifier is None
            or evidence.provider_device_identifier_sha256
            != hashlib.sha256(
                row.device_identifier.encode("utf-8")
            ).hexdigest()
        ):
            missing.append("binding_device_mismatch")
        if evidence is None or not evidence.compatibility_profile_id:
            missing.append("compatibility_profile_unconfirmed")
        if not missing:
            return (
                "normalization_pending",
                self.confirmed_namespace,
                None,
                None,
            )
        reason = _quarantine_reason(evidence, missing)
        return (
            "quarantined",
            self.quarantine_namespace,
            reason,
            "legacy_" + ",".join(missing),
        )

    def _require_provider_accounts(self, created_at: datetime) -> None:
        confirmed = self.repository.get_provider_account(
            self.confirmed_namespace,
            provider_account_id=self.provider_account_id,
        )
        if confirmed is None or confirmed.provider_id != "perceptor":
            raise LegacyImportError(
                "confirmed Perceptor provider account must already exist"
            )
        quarantine = self.repository.get_provider_account(
            self.quarantine_namespace,
            provider_account_id=self.provider_account_id,
        )
        if quarantine is None:
            self.repository.save_provider_account(
                ProviderAccountRecord(
                    namespace=self.quarantine_namespace,
                    provider_account_id=self.provider_account_id,
                    provider_id="perceptor",
                    configuration_fingerprint=(
                        confirmed.configuration_fingerprint
                    ),
                    status="enabled",
                    metadata={
                        "environment": "legacy-quarantine",
                        "authoritative": False,
                    },
                    created_at=created_at,
                )
            )
        elif quarantine.provider_id != "perceptor":
            raise LegacyImportError("quarantine provider account is not Perceptor")

    def _begin_run(
        self,
        *,
        run_id: str,
        source_fingerprint: str,
        manifest_sha256: str,
        source_record_count: int,
        started_at: datetime,
    ) -> LegacyImportReport | None:
        with self.store.transaction_lock:
            cursor = self.store.connection.cursor()
            try:
                if self.store.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    _sql(
                        self.store,
                        """
                        SELECT status, source_record_count,
                               imported_record_count,
                               quarantined_record_count,
                               duplicate_record_count
                        FROM sleep_domain_legacy_import_runs
                        WHERE import_run_id = ?
                        """
                    ),
                    (run_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == "succeeded":
                        self.store.connection.commit()
                        return LegacyImportReport(
                            import_run_id=run_id,
                            source_fingerprint_sha256=source_fingerprint,
                            manifest_sha256=manifest_sha256,
                            source_record_count=int(row[1]),
                            imported_record_count=int(row[2]),
                            quarantined_record_count=int(row[3]),
                            duplicate_record_count=int(row[4]),
                            status="succeeded",
                        )
                    if str(row[0]) == "running":
                        raise LegacyImportInProgressError(
                            "the same legacy import is already running"
                        )
                    cursor.execute(
                        _sql(
                            self.store,
                            """
                            UPDATE sleep_domain_legacy_import_runs
                            SET status = 'running', failure_code = NULL,
                                completed_at = NULL, started_at = ?
                            WHERE import_run_id = ?
                            """
                        ),
                        (_dt(started_at), run_id),
                    )
                else:
                    cursor.execute(
                        _sql(
                            self.store,
                            """
                            INSERT INTO sleep_domain_legacy_import_runs (
                              import_run_id, source_fingerprint_sha256,
                              manifest_sha256, confirmed_namespace_id,
                              confirmed_data_mode, quarantine_namespace_id,
                              status, source_record_count,
                              imported_record_count, quarantined_record_count,
                              duplicate_record_count, failure_code,
                              started_at, completed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, 0, 0, 0,
                                      NULL, ?, NULL)
                            """
                        ),
                        (
                            run_id,
                            source_fingerprint,
                            manifest_sha256,
                            self.confirmed_namespace.namespace_id,
                            self.confirmed_namespace.data_mode.value,
                            self.quarantine_namespace.namespace_id,
                            source_record_count,
                            _dt(started_at),
                        ),
                    )
                self.store.connection.commit()
                return None
            except Exception:
                self.store.connection.rollback()
                raise
            finally:
                cursor.close()

    def _record_disposition(
        self,
        *,
        run_id: str,
        source_record_key: str,
    ) -> str | None:
        with self.store.transaction_lock:
            row = self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    SELECT disposition
                    FROM sleep_domain_legacy_import_records
                    WHERE import_run_id = ? AND source_record_key = ?
                    """
                ),
                (run_id, source_record_key),
            ).fetchone()
        return None if row is None else str(row[0])

    def _await_run(
        self,
        *,
        run_id: str,
        source_fingerprint: str,
        manifest_sha256: str,
        timeout_seconds: float,
    ) -> LegacyImportReport:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self.store.transaction_lock:
                row = self.store.connection.execute(
                    _sql(
                        self.store,
                        """
                        SELECT status, source_record_count,
                               imported_record_count,
                               quarantined_record_count,
                               duplicate_record_count, failure_code
                        FROM sleep_domain_legacy_import_runs
                        WHERE import_run_id = ?
                        """,
                    ),
                    (run_id,),
                ).fetchone()
            if row is not None and str(row[0]) == "succeeded":
                return LegacyImportReport(
                    import_run_id=run_id,
                    source_fingerprint_sha256=source_fingerprint,
                    manifest_sha256=manifest_sha256,
                    source_record_count=int(row[1]),
                    imported_record_count=int(row[2]),
                    quarantined_record_count=int(row[3]),
                    duplicate_record_count=int(row[4]),
                    status="succeeded",
                )
            if row is not None and str(row[0]) == "failed":
                raise LegacyImportError(
                    "concurrent legacy import failed with code "
                    f"{str(row[5] or 'unknown')}"
                )
            time.sleep(0.01)
        raise LegacyImportInProgressError(
            "the same legacy import is still running"
        )

    def _save_record(
        self,
        *,
        run_id: str,
        row: _LegacyRawRow,
        target: DomainNamespace,
        raw_id: str,
        payload_sha256: str,
        disposition: str,
        reason: QuarantineReason | None,
        detail_code: str | None,
        imported_at: datetime,
    ) -> None:
        with self.store.transaction_lock:
            self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    INSERT INTO sleep_domain_legacy_import_records (
                      import_run_id, source_record_key, legacy_raw_event_id,
                      target_namespace_id, target_data_mode,
                      raw_ingress_record_id, payload_sha256,
                      legacy_normalized_event_type,
                      legacy_normalized_payload_sha256, disposition,
                      quarantine_reason, detail_code, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (import_run_id, source_record_key) DO NOTHING
                    """
                ),
                (
                    run_id,
                    row.source_record_key,
                    row.legacy_raw_event_id,
                    target.namespace_id,
                    target.data_mode.value,
                    raw_id,
                    payload_sha256,
                    row.normalized_event_type,
                    row.normalized_payload_sha256,
                    disposition,
                    None if reason is None else reason.value,
                    detail_code,
                    _dt(imported_at),
                ),
            )
            self.store.connection.commit()

    def _finish_run(
        self,
        report: LegacyImportReport,
        *,
        completed_at: datetime,
    ) -> None:
        with self.store.transaction_lock:
            self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    UPDATE sleep_domain_legacy_import_runs
                    SET status = 'succeeded', imported_record_count = ?,
                        quarantined_record_count = ?,
                        duplicate_record_count = ?, completed_at = ?
                    WHERE import_run_id = ? AND status = 'running'
                    """
                ),
                (
                    report.imported_record_count,
                    report.quarantined_record_count,
                    report.duplicate_record_count,
                    _dt(completed_at),
                    report.import_run_id,
                ),
            )
            self.store.connection.commit()

    def _fail_run(
        self,
        run_id: str,
        *,
        failure_code: str,
        completed_at: datetime,
    ) -> None:
        with self.store.transaction_lock:
            self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    UPDATE sleep_domain_legacy_import_runs
                    SET status = 'failed', failure_code = ?, completed_at = ?
                    WHERE import_run_id = ?
                    """
                ),
                (failure_code[:100], _dt(completed_at), run_id),
            )
            self.store.connection.commit()


def _read_legacy_rows(path: Path) -> tuple[_LegacyRawRow, ...]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "perceptor_raw_events" not in tables:
            raise LegacyImportError(
                "legacy SQLite lacks perceptor_raw_events"
            )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(perceptor_raw_events)"
            ).fetchall()
        }
        required = {
            "id",
            "raw_event_id",
            "message_id",
            "event_type",
            "device_identifier",
            "received_at",
            "raw_payload_json",
            "normalization_status",
        }
        if not required.issubset(columns):
            raise LegacyImportError(
                "legacy perceptor_raw_events schema is unsupported"
            )
        vendor_expression = (
            "raw.vendor AS vendor"
            if "vendor" in columns
            else "'perceptor' AS vendor"
        )
        normalized_available = "perceptor_normalized_events" in tables
        normalized_select = (
            """
            normalized.normalized_event_type AS normalized_event_type,
            normalized.normalized_payload_json AS normalized_payload_json,
            """
            if normalized_available
            else """
            NULL AS normalized_event_type,
            NULL AS normalized_payload_json,
            """
        )
        normalized_join = (
            """
            LEFT JOIN perceptor_normalized_events AS normalized
              ON normalized.raw_event_id = raw.raw_event_id
            """
            if normalized_available
            else ""
        )
        rows = connection.execute(
            f"""
            SELECT raw.id, raw.raw_event_id, {vendor_expression},
                   raw.message_id, raw.event_type, raw.device_identifier,
                   raw.received_at, raw.raw_payload_json,
                   raw.normalization_status, {normalized_select}
                   1 AS raw_payload_available
            FROM perceptor_raw_events AS raw
            {normalized_join}
            ORDER BY raw.id
            """
        ).fetchall()
        result = [
            _LegacyRawRow(
                source_record_key=f"perceptor_raw_events:{int(row['id'])}",
                legacy_raw_event_id=str(row["raw_event_id"]),
                vendor=str(row["vendor"] or "perceptor"),
                message_id=(
                    None if row["message_id"] is None else str(row["message_id"])
                ),
                event_type=str(row["event_type"] or "UnknownEvent"),
                device_identifier=(
                    None
                    if row["device_identifier"] is None
                    else str(row["device_identifier"])
                ),
                received_at_text=str(row["received_at"]),
                raw_payload_text=str(row["raw_payload_json"]),
                normalization_status=str(row["normalization_status"]),
                normalized_event_type=(
                    None
                    if row["normalized_event_type"] is None
                    else str(row["normalized_event_type"])
                ),
                normalized_payload_sha256=(
                    None
                    if row["normalized_payload_json"] is None
                    else hashlib.sha256(
                        str(row["normalized_payload_json"]).encode("utf-8")
                    ).hexdigest()
                ),
            )
            for row in rows
        ]
        if normalized_available:
            normalized_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(perceptor_normalized_events)"
                ).fetchall()
            }
            normalized_vendor = (
                "normalized.vendor AS vendor"
                if "vendor" in normalized_columns
                else "'perceptor' AS vendor"
            )
            normalized_device = (
                "normalized.device_identifier AS device_identifier"
                if "device_identifier" in normalized_columns
                else "NULL AS device_identifier"
            )
            orphans = connection.execute(
                f"""
                SELECT normalized.id, normalized.raw_event_id,
                       {normalized_vendor}, normalized.message_id,
                       normalized.event_type, {normalized_device},
                       normalized.normalized_at,
                       normalized.normalized_event_type,
                       normalized.normalized_payload_json
                FROM perceptor_normalized_events AS normalized
                LEFT JOIN perceptor_raw_events AS raw
                  ON raw.raw_event_id = normalized.raw_event_id
                WHERE raw.raw_event_id IS NULL
                ORDER BY normalized.id
                """
            ).fetchall()
            result.extend(
                _LegacyRawRow(
                    source_record_key=(
                        f"perceptor_normalized_events:{int(row['id'])}"
                    ),
                    legacy_raw_event_id=str(row["raw_event_id"]),
                    vendor=str(row["vendor"] or "perceptor"),
                    message_id=(
                        None
                        if row["message_id"] is None
                        else str(row["message_id"])
                    ),
                    event_type=str(row["event_type"] or "UnknownEvent"),
                    device_identifier=(
                        None
                        if row["device_identifier"] is None
                        else str(row["device_identifier"])
                    ),
                    received_at_text=str(row["normalized_at"]),
                    raw_payload_text=str(row["normalized_payload_json"]),
                    normalization_status="orphan_normalized",
                    normalized_event_type=str(row["normalized_event_type"]),
                    normalized_payload_sha256=hashlib.sha256(
                        str(row["normalized_payload_json"]).encode("utf-8")
                    ).hexdigest(),
                    raw_payload_available=False,
                )
                for row in orphans
            )
        return tuple(result)
    finally:
        connection.close()


def _source_fingerprint(rows: tuple[_LegacyRawRow, ...]) -> str:
    material = [
        {
            "source_record_key": row.source_record_key,
            "legacy_raw_event_id": row.legacy_raw_event_id,
            "vendor": row.vendor,
            "message_id": row.message_id,
            "event_type": row.event_type,
            "device_identifier": row.device_identifier,
            "received_at": row.received_at_text,
            "raw_payload_sha256": hashlib.sha256(
                row.raw_payload_text.encode("utf-8")
            ).hexdigest(),
            "normalization_status": row.normalization_status,
            "normalized_event_type": row.normalized_event_type,
            "normalized_payload_sha256": row.normalized_payload_sha256,
            "raw_payload_available": row.raw_payload_available,
        }
        for row in rows
    ]
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _legacy_received_at(
    text: str,
    *,
    fallback: datetime,
) -> tuple[datetime, bool]:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return fallback, False
    return parsed.astimezone(UTC), True


def _quarantine_reason(
    evidence: LegacyRecordEvidence | None,
    missing: list[str],
) -> QuarantineReason:
    if evidence is not None and (
        evidence.signature_verification == SignatureVerificationState.REJECTED
    ):
        return QuarantineReason.INVALID_SIGNATURE
    if any(item.startswith("data_mode") for item in missing):
        return QuarantineReason.UNKNOWN
    if "signature_unconfirmed" in missing:
        return QuarantineReason.UNKNOWN
    if "event_time_unconfirmed" in missing:
        return QuarantineReason.UNPARSEABLE_TIME
    if "binding_unconfirmed" in missing:
        return QuarantineReason.DEVICE_UNBOUND
    return QuarantineReason.UNKNOWN


def _quarantine_stage(reason: QuarantineReason) -> ProcessingStage:
    if reason == QuarantineReason.INVALID_SIGNATURE:
        return ProcessingStage.AUTHENTICATION
    if reason in {
        QuarantineReason.DEVICE_UNBOUND,
        QuarantineReason.DEVICE_BINDING_AMBIGUOUS,
    }:
        return ProcessingStage.BINDING
    return ProcessingStage.INTAKE


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(
        "|".join((prefix, *parts)).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _sql(repository_store: Any, sql: str) -> str:
    return sql if repository_store.dialect == "sqlite" else sql.replace("?", "%s")


def _dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


__all__ = [
    "IMPORTER_VERSION",
    "LEGACY_QUARANTINE_NAMESPACE_MARKER",
    "LegacyImportError",
    "LegacyImportInProgressError",
    "LegacyImportManifest",
    "LegacyImportReport",
    "LegacyRecordEvidence",
    "LegacyWebhookImporter",
]
