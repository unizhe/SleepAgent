from __future__ import annotations

import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sleepagent.integrations.perceptor.client import (
    PERCEPTOR_CLIENT_SECRET_ENV,
    PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND_ENV,
)
from sleepagent.integrations.perceptor.signing import sign_parameters
from sleepagent.product_device import (
    DEFAULT_RADAR_VENDOR,
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarDevice,
    RadarDeviceStatus,
    RawVendorEvent,
    RawVendorEventNormalizationStatus,
    build_raw_vendor_event,
    build_vital_snapshot_from_raw_event,
)
PERCEPTOR_WEBHOOK_SECRET_ENV = "PERCEPTOR_WEBHOOK_SECRET"
PERCEPTOR_WEBHOOK_ALLOW_UNSIGNED_DEV_ONLY_ENV = (
    "PERCEPTOR_WEBHOOK_ALLOW_UNSIGNED_DEV_ONLY"
)
PERCEPTOR_WEBHOOK_SIGNING_PATH_ENV = "PERCEPTOR_WEBHOOK_SIGNING_PATH"
PERCEPTOR_WEBHOOK_STORE_DIR_ENV = "PERCEPTOR_WEBHOOK_STORE_DIR"
PERCEPTOR_WEBHOOK_DB_FILENAME = "perceptor_webhook_events.sqlite3"
DEFAULT_PERCEPTOR_WEBHOOK_SIGNING_PATH = "/integrations/perceptor/webhook"
DEFAULT_PERCEPTOR_WEBHOOK_STORE_DIR = "/tmp/sleepagent_perceptor_webhook"
LEGACY_WEBHOOK_DIAGNOSTIC_ENV = "SLEEPAGENT_LEGACY_PERCEPTOR_DIAGNOSTIC"
SQLITE_TIMEOUT_SECONDS = 5.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5_000

SUPPORTED_WEBHOOK_EVENT_TYPES = {
    "VitalSignsDataEvent",
    "AlarmEvent",
    "AlarmStopEvent",
    "ConnectedEvent",
    "DisconnectedEvent",
}


class PerceptorWebhookError(RuntimeError):
    """Base error for Perceptor webhook ingestion failures."""


class PerceptorWebhookConfigurationError(PerceptorWebhookError):
    """Raised when webhook verification cannot be configured safely."""


class PerceptorWebhookSignatureError(PerceptorWebhookError):
    """Raised when webhook signature verification fails."""


@dataclass(frozen=True)
class PerceptorWebhookConfig:
    signing_secret: str | None
    allow_unsigned_dev_only: bool = False
    signing_path: str = DEFAULT_PERCEPTOR_WEBHOOK_SIGNING_PATH
    append_ampersand_to_signing_secret: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PerceptorWebhookConfig":
        values = env or os.environ
        signing_secret = _optional_env(values, PERCEPTOR_WEBHOOK_SECRET_ENV) or _optional_env(
            values,
            PERCEPTOR_CLIENT_SECRET_ENV,
        )
        return cls(
            signing_secret=signing_secret,
            allow_unsigned_dev_only=_bool_env(
                values,
                PERCEPTOR_WEBHOOK_ALLOW_UNSIGNED_DEV_ONLY_ENV,
                False,
            ),
            signing_path=values.get(
                PERCEPTOR_WEBHOOK_SIGNING_PATH_ENV,
                DEFAULT_PERCEPTOR_WEBHOOK_SIGNING_PATH,
            ),
            append_ampersand_to_signing_secret=_bool_env(
                values,
                PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND_ENV,
                True,
            ),
        )


@dataclass(frozen=True)
class PerceptorWebhookIngestResult:
    success: bool
    duplicate: bool
    message_id: str | None
    event_type: str | None
    normalization_status: str
    raw_event_id: str | None = None
    normalized_event_type: str | None = None
    unnormalized_reason: str | None = None

    def to_response_payload(self) -> dict[str, Any]:
        return {
            "code": 200,
            "success": self.success,
            "message": "OK",
            "data": {
                "duplicate": self.duplicate,
                "message_id": self.message_id,
                "event_type": self.event_type,
                "normalization_status": self.normalization_status,
                "raw_event_id": self.raw_event_id,
                "normalized_event_type": self.normalized_event_type,
                "unnormalized_reason": self.unnormalized_reason,
            },
        }


class PerceptorWebhookRepository:
    """Deprecated local/test store; never a production authority."""

    def __init__(self, db_path: str | Path) -> None:
        if (
            os.getenv("SLEEPAGENT_DEPLOYMENT_MODE", "development")
            .strip()
            .lower()
            == "production"
        ):
            raise PerceptorWebhookConfigurationError(
                "legacy webhook SQLite is disabled in production; use the "
                "unified FastAPI ingestion service"
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def insert_raw_event(
        self,
        *,
        raw_event_id: str,
        vendor: str,
        message_id: str | None,
        event_type: str | None,
        device_identifier: str | None,
        received_at: datetime,
        raw_payload: Mapping[str, Any],
        data_payload: Mapping[str, Any] | None,
        normalization_status: str,
        unnormalized_reason: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO perceptor_raw_events (
                    raw_event_id,
                    vendor,
                    message_id,
                    event_type,
                    device_identifier,
                    received_at,
                    raw_payload_json,
                    data_payload_json,
                    normalization_status,
                    unnormalized_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_event_id,
                    vendor,
                    message_id,
                    event_type,
                    device_identifier,
                    _format_datetime(received_at),
                    _json_dumps(dict(raw_payload)),
                    _json_dumps(dict(data_payload or {})),
                    normalization_status,
                    unnormalized_reason,
                ),
            )
            return cursor.rowcount == 1

    def update_raw_normalization_status(
        self,
        *,
        vendor: str,
        device_identifier: str | None,
        event_type: str | None,
        message_id: str | None,
        raw_event_id: str,
        normalization_status: str,
        unnormalized_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            if message_id:
                connection.execute(
                    """
                    UPDATE perceptor_raw_events
                    SET normalization_status = ?, unnormalized_reason = ?
                    WHERE vendor = ?
                      AND COALESCE(device_identifier, '') = COALESCE(?, '')
                      AND COALESCE(event_type, '') = COALESCE(?, '')
                      AND COALESCE(message_id, '') = COALESCE(?, '')
                    """,
                    (
                        normalization_status,
                        unnormalized_reason,
                        vendor,
                        device_identifier,
                        event_type,
                        message_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE perceptor_raw_events
                    SET normalization_status = ?, unnormalized_reason = ?
                    WHERE raw_event_id = ?
                    """,
                    (normalization_status, unnormalized_reason, raw_event_id),
                )

    def insert_normalized_event(
        self,
        *,
        raw_event_id: str,
        vendor: str,
        device_identifier: str | None,
        message_id: str | None,
        event_type: str,
        normalized_event_type: str,
        normalized_payload: Mapping[str, Any],
        normalized_at: datetime,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO perceptor_normalized_events (
                    raw_event_id,
                    vendor,
                    device_identifier,
                    message_id,
                    event_type,
                    normalized_event_type,
                    normalized_payload_json,
                    normalized_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_event_id,
                    vendor,
                    device_identifier,
                    message_id,
                    event_type,
                    normalized_event_type,
                    _json_dumps(dict(normalized_payload)),
                    _format_datetime(normalized_at),
                ),
            )
            return cursor.rowcount == 1

    def get_raw_event_by_identity(
        self,
        *,
        vendor: str,
        device_identifier: str | None,
        event_type: str | None,
        message_id: str | None,
    ) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT raw_event_id, vendor, message_id, event_type, device_identifier,
                       received_at, raw_payload_json, data_payload_json,
                       normalization_status, unnormalized_reason
                FROM perceptor_raw_events
                WHERE vendor = ?
                  AND COALESCE(device_identifier, '') = COALESCE(?, '')
                  AND COALESCE(event_type, '') = COALESCE(?, '')
                  AND COALESCE(message_id, '') = COALESCE(?, '')
                """,
                (vendor, device_identifier, event_type, message_id),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_normalized_event_by_identity(
        self,
        *,
        vendor: str,
        device_identifier: str | None,
        event_type: str | None,
        message_id: str | None,
    ) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT raw_event_id, vendor, device_identifier, message_id, event_type,
                       normalized_event_type, normalized_payload_json, normalized_at
                FROM perceptor_normalized_events
                WHERE vendor = ?
                  AND COALESCE(device_identifier, '') = COALESCE(?, '')
                  AND COALESCE(event_type, '') = COALESCE(?, '')
                  AND COALESCE(message_id, '') = COALESCE(?, '')
                """,
                (vendor, device_identifier, event_type, message_id),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_raw_event_by_message_id(self, message_id: str | None) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT raw_event_id, vendor, message_id, event_type, device_identifier,
                       received_at, raw_payload_json, data_payload_json,
                       normalization_status, unnormalized_reason
                FROM perceptor_raw_events
                WHERE message_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_normalized_event_by_message_id(
        self,
        message_id: str | None,
    ) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT raw_event_id, vendor, device_identifier, message_id, event_type,
                       normalized_event_type, normalized_payload_json, normalized_at
                FROM perceptor_normalized_events
                WHERE message_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_raw_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT raw_event_id, vendor, message_id, event_type, device_identifier,
                       received_at, raw_payload_json, data_payload_json,
                       normalization_status, unnormalized_reason
                FROM perceptor_raw_events
                ORDER BY id
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_normalized_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT raw_event_id, vendor, device_identifier, message_id, event_type,
                       normalized_event_type, normalized_payload_json, normalized_at
                FROM perceptor_normalized_events
                ORDER BY id
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=SQLITE_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS perceptor_raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_event_id TEXT NOT NULL UNIQUE,
                    vendor TEXT NOT NULL DEFAULT 'perceptor',
                    message_id TEXT,
                    event_type TEXT,
                    device_identifier TEXT,
                    received_at TEXT NOT NULL,
                    raw_payload_json TEXT NOT NULL,
                    data_payload_json TEXT NOT NULL,
                    normalization_status TEXT NOT NULL,
                    unnormalized_reason TEXT
                )
                """
            )
            _ensure_sqlite_column(
                connection,
                table_name="perceptor_raw_events",
                column_name="vendor",
                column_sql="vendor TEXT NOT NULL DEFAULT 'perceptor'",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS perceptor_normalized_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_event_id TEXT NOT NULL,
                    vendor TEXT NOT NULL DEFAULT 'perceptor',
                    device_identifier TEXT,
                    message_id TEXT,
                    event_type TEXT NOT NULL,
                    normalized_event_type TEXT NOT NULL,
                    normalized_payload_json TEXT NOT NULL,
                    normalized_at TEXT NOT NULL
                )
                """
            )
            _ensure_sqlite_column(
                connection,
                table_name="perceptor_normalized_events",
                column_name="vendor",
                column_sql="vendor TEXT NOT NULL DEFAULT 'perceptor'",
            )
            _ensure_sqlite_column(
                connection,
                table_name="perceptor_normalized_events",
                column_name="device_identifier",
                column_sql="device_identifier TEXT",
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_perceptor_raw_event_identity
                ON perceptor_raw_events(
                    vendor,
                    COALESCE(device_identifier, ''),
                    COALESCE(event_type, ''),
                    COALESCE(message_id, '')
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_perceptor_normalized_event_identity
                ON perceptor_normalized_events(
                    vendor,
                    COALESCE(device_identifier, ''),
                    COALESCE(event_type, ''),
                    COALESCE(message_id, '')
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_perceptor_raw_message_id
                ON perceptor_raw_events(message_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_perceptor_normalized_message_id
                ON perceptor_normalized_events(message_id)
                """
            )


class PerceptorWebhookIngestionService:
    def __init__(
        self,
        *,
        config: PerceptorWebhookConfig,
        repository: PerceptorWebhookRepository,
    ) -> None:
        self.config = config
        self.repository = repository

    def ingest(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        received_at: datetime | None = None,
    ) -> PerceptorWebhookIngestResult:
        verify_perceptor_webhook_signature(
            payload,
            headers=headers or {},
            config=self.config,
        )
        resolved_received_at = received_at or datetime.now(timezone.utc)
        raw_payload = dict(payload)
        payload_identity = _payload_event_identity(raw_payload)
        message_id = payload_identity["message_id"]
        existing = self.repository.get_raw_event_by_identity(**payload_identity)
        if existing is not None:
            normalized = self.repository.get_normalized_event_by_identity(
                **payload_identity
            )
            return PerceptorWebhookIngestResult(
                success=True,
                duplicate=True,
                message_id=message_id,
                event_type=existing.get("event_type"),
                normalization_status=existing["normalization_status"],
                raw_event_id=existing["raw_event_id"],
                normalized_event_type=(
                    normalized.get("normalized_event_type") if normalized else None
                ),
                unnormalized_reason=existing.get("unnormalized_reason"),
            )

        try:
            raw_event = build_raw_vendor_event(
                raw_payload,
                received_at=resolved_received_at,
            )
        except ValueError as exc:
            raw_event_id = _fallback_raw_event_id(raw_payload, message_id)
            event_type = _optional_string(raw_payload.get("type"))
            reason = str(exc)
            inserted = self.repository.insert_raw_event(
                raw_event_id=raw_event_id,
                vendor=payload_identity["vendor"],
                message_id=message_id,
                event_type=event_type,
                device_identifier=payload_identity["device_identifier"],
                received_at=resolved_received_at,
                raw_payload=raw_payload,
                data_payload={},
                normalization_status=RawVendorEventNormalizationStatus.UNNORMALIZED.value,
                unnormalized_reason=reason,
            )
            if not inserted:
                existing_after_insert = self.repository.get_raw_event_by_identity(
                    **payload_identity
                )
                return PerceptorWebhookIngestResult(
                    success=True,
                    duplicate=True,
                    message_id=message_id,
                    event_type=(
                        existing_after_insert.get("event_type")
                        if existing_after_insert
                        else event_type
                    ),
                    normalization_status=(
                        existing_after_insert["normalization_status"]
                        if existing_after_insert
                        else RawVendorEventNormalizationStatus.UNNORMALIZED.value
                    ),
                    raw_event_id=(
                        existing_after_insert["raw_event_id"]
                        if existing_after_insert
                        else raw_event_id
                    ),
                    unnormalized_reason=(
                        existing_after_insert.get("unnormalized_reason")
                        if existing_after_insert
                        else reason
                    ),
                )
            return PerceptorWebhookIngestResult(
                success=True,
                duplicate=False,
                message_id=message_id,
                event_type=event_type,
                normalization_status=RawVendorEventNormalizationStatus.UNNORMALIZED.value,
                raw_event_id=raw_event_id,
                unnormalized_reason=reason,
            )

        raw_identity = _raw_event_identity(raw_event)
        inserted = self.repository.insert_raw_event(
            raw_event_id=raw_event.raw_event_id,
            vendor=raw_identity["vendor"],
            message_id=raw_event.message_id,
            event_type=raw_event.event_type,
            device_identifier=raw_event.device_identifier,
            received_at=raw_event.received_at,
            raw_payload=raw_event.raw_payload,
            data_payload=raw_event.data_payload,
            normalization_status=RawVendorEventNormalizationStatus.RAW_ONLY.value,
        )
        if not inserted:
            existing_after_insert = self.repository.get_raw_event_by_identity(
                **raw_identity
            )
            normalized = self.repository.get_normalized_event_by_identity(
                **raw_identity
            )
            return PerceptorWebhookIngestResult(
                success=True,
                duplicate=True,
                message_id=raw_event.message_id,
                event_type=(
                    existing_after_insert.get("event_type")
                    if existing_after_insert
                    else raw_event.event_type
                ),
                normalization_status=(
                    existing_after_insert["normalization_status"]
                    if existing_after_insert
                    else RawVendorEventNormalizationStatus.RAW_ONLY.value
                ),
                raw_event_id=(
                    existing_after_insert["raw_event_id"]
                    if existing_after_insert
                    else raw_event.raw_event_id
                ),
                normalized_event_type=(
                    normalized.get("normalized_event_type") if normalized else None
                ),
                unnormalized_reason=(
                    existing_after_insert.get("unnormalized_reason")
                    if existing_after_insert
                    else None
                ),
            )

        try:
            normalized_event_type, normalized_payload = normalize_perceptor_webhook_event(
                raw_event
            )
        except ValueError as exc:
            reason = str(exc)
            self.repository.update_raw_normalization_status(
                vendor=raw_identity["vendor"],
                device_identifier=raw_identity["device_identifier"],
                event_type=raw_identity["event_type"],
                message_id=raw_event.message_id,
                raw_event_id=raw_event.raw_event_id,
                normalization_status=RawVendorEventNormalizationStatus.UNNORMALIZED.value,
                unnormalized_reason=reason,
            )
            return PerceptorWebhookIngestResult(
                success=True,
                duplicate=False,
                message_id=raw_event.message_id,
                event_type=raw_event.event_type,
                normalization_status=RawVendorEventNormalizationStatus.UNNORMALIZED.value,
                raw_event_id=raw_event.raw_event_id,
                unnormalized_reason=reason,
            )

        self.repository.insert_normalized_event(
            raw_event_id=raw_event.raw_event_id,
            vendor=raw_identity["vendor"],
            device_identifier=raw_identity["device_identifier"],
            message_id=raw_event.message_id,
            event_type=raw_event.event_type,
            normalized_event_type=normalized_event_type,
            normalized_payload=normalized_payload,
            normalized_at=datetime.now(timezone.utc),
        )
        self.repository.update_raw_normalization_status(
            vendor=raw_identity["vendor"],
            device_identifier=raw_identity["device_identifier"],
            event_type=raw_identity["event_type"],
            message_id=raw_event.message_id,
            raw_event_id=raw_event.raw_event_id,
            normalization_status=RawVendorEventNormalizationStatus.NORMALIZED.value,
        )
        return PerceptorWebhookIngestResult(
            success=True,
            duplicate=False,
            message_id=raw_event.message_id,
            event_type=raw_event.event_type,
            normalization_status=RawVendorEventNormalizationStatus.NORMALIZED.value,
            raw_event_id=raw_event.raw_event_id,
            normalized_event_type=normalized_event_type,
        )


def build_perceptor_webhook_service_from_env(
    env: Mapping[str, str] | None = None,
) -> PerceptorWebhookIngestionService:
    values = env or os.environ
    if (
        str(values.get(LEGACY_WEBHOOK_DIAGNOSTIC_ENV, "false"))
        .strip()
        .lower()
        != "true"
        or str(values.get("SLEEPAGENT_DEPLOYMENT_MODE", "development"))
        .strip()
        .lower()
        == "production"
    ):
        raise PerceptorWebhookConfigurationError(
            "legacy webhook service is an explicit non-production diagnostic "
            "only; use the unified FastAPI ingestion service"
        )
    store_dir = Path(
        values.get(
            PERCEPTOR_WEBHOOK_STORE_DIR_ENV,
            DEFAULT_PERCEPTOR_WEBHOOK_STORE_DIR,
        )
    )
    return PerceptorWebhookIngestionService(
        config=PerceptorWebhookConfig.from_env(values),
        repository=PerceptorWebhookRepository(store_dir / PERCEPTOR_WEBHOOK_DB_FILENAME),
    )


def verify_perceptor_webhook_signature(
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    config: PerceptorWebhookConfig,
) -> None:
    signature = _extract_signature(payload, headers)
    if config.allow_unsigned_dev_only and not signature:
        return
    if not signature:
        raise PerceptorWebhookSignatureError("Perceptor webhook signature is required.")
    if not config.signing_secret:
        raise PerceptorWebhookConfigurationError(
            "Perceptor webhook signing secret is not configured."
        )
    expected_signature = sign_parameters(
        _normalize_webhook_signing_params(payload),
        client_secret=config.signing_secret,
        append_ampersand=config.append_ampersand_to_signing_secret,
        signing_path=config.signing_path,
    )
    if not hmac.compare_digest(str(signature), expected_signature):
        raise PerceptorWebhookSignatureError("Invalid Perceptor webhook signature.")


def sign_perceptor_webhook_payload(
    payload: Mapping[str, Any],
    *,
    client_secret: str,
    signing_path: str = DEFAULT_PERCEPTOR_WEBHOOK_SIGNING_PATH,
    append_ampersand: bool = True,
) -> str:
    return sign_parameters(
        _normalize_webhook_signing_params(payload),
        client_secret=client_secret,
        append_ampersand=append_ampersand,
        signing_path=signing_path,
    )


def normalize_perceptor_webhook_event(
    raw_event: RawVendorEvent,
) -> tuple[str, dict[str, Any]]:
    event_type = raw_event.event_type
    if event_type not in SUPPORTED_WEBHOOK_EVENT_TYPES:
        raise ValueError(f"Unsupported Perceptor webhook event type: {event_type}.")
    if event_type == "VitalSignsDataEvent":
        snapshot = build_vital_snapshot_from_raw_event(raw_event)
        return "radar_vital_snapshot", _model_to_payload(snapshot)
    if event_type in {"AlarmEvent", "AlarmStopEvent"}:
        alert = _build_alert_event(raw_event)
        normalized_type = (
            "radar_alarm_event"
            if event_type == "AlarmEvent"
            else "radar_alarm_stop_event"
        )
        return normalized_type, _model_to_payload(alert)
    if event_type in {"ConnectedEvent", "DisconnectedEvent"}:
        device = _build_device_event(raw_event)
        return "radar_device", _model_to_payload(device)
    raise ValueError(f"Unsupported Perceptor webhook event type: {event_type}.")


def _build_alert_event(raw_event: RawVendorEvent) -> RadarAlertEvent:
    data = raw_event.data_payload
    occurred_at = raw_event.event_timestamp or raw_event.received_at
    alert_type = _optional_string(
        data.get("alarm_type")
        or data.get("alarmType")
        or data.get("alarm_code")
        or data.get("alarmCode")
        or data.get("code")
        or raw_event.event_type
    )
    return RadarAlertEvent(
        radar_alert_event_id=f"perceptor-alert:{raw_event.message_id or raw_event.raw_event_id}",
        radar_device_id=raw_event.device_identifier or raw_event.raw_event_id,
        alert_type=alert_type or raw_event.event_type,
        severity=RadarAlertSeverity.UNKNOWN,
        occurred_at=occurred_at,
        resolved_at=occurred_at if raw_event.event_type == "AlarmStopEvent" else None,
        title=_optional_string(data.get("title") or data.get("name")),
        message=_optional_string(data.get("message") or data.get("msg")),
        source_metadata=raw_event.to_source_metadata(),
    )


def _build_device_event(raw_event: RawVendorEvent) -> RadarDevice:
    status = (
        RadarDeviceStatus.ONLINE
        if raw_event.event_type == "ConnectedEvent"
        else RadarDeviceStatus.OFFLINE
    )
    device_identifier = raw_event.device_identifier or raw_event.raw_event_id
    return RadarDevice(
        radar_device_id=device_identifier,
        display_name=device_identifier,
        provider=DEFAULT_RADAR_VENDOR,
        status=status,
        vendor_device_id=raw_event.device_id,
        vendor_device_name=raw_event.device_name,
        vendor_home_id=raw_event.home_id,
        source_metadata=raw_event.to_source_metadata(),
        registered_at=raw_event.received_at,
        updated_at=raw_event.received_at,
    )


def _normalize_webhook_signing_params(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _normalize_webhook_signing_value(value)
        for key, value in payload.items()
    }


def _normalize_webhook_signing_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return value


def _extract_signature(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
) -> str | None:
    for key in ("sign", "signature"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    for key in (
        "x-perceptor-signature",
        "x-perceptor-sign",
        "x-signature",
        "x-sign",
        "sign",
        "signature",
    ):
        value = normalized_headers.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _extract_message_id(payload: Mapping[str, Any]) -> str | None:
    return _optional_string(payload.get("message_id") or payload.get("messageId"))


def _payload_event_identity(payload: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "vendor": DEFAULT_RADAR_VENDOR,
        "device_identifier": _extract_device_identifier(payload),
        "event_type": _optional_string(payload.get("type")),
        "message_id": _extract_message_id(payload),
    }


def _raw_event_identity(raw_event: RawVendorEvent) -> dict[str, str | None]:
    return {
        "vendor": raw_event.vendor,
        "device_identifier": raw_event.device_identifier,
        "event_type": raw_event.event_type,
        "message_id": raw_event.message_id,
    }


def _extract_device_identifier(payload: Mapping[str, Any]) -> str | None:
    data = payload.get("data")
    data_payload = data if isinstance(data, Mapping) else {}
    return _optional_string(
        payload.get("device_name")
        or payload.get("deviceName")
        or payload.get("device_id")
        or payload.get("deviceId")
        or data_payload.get("device_name")
        or data_payload.get("deviceName")
    )


def _fallback_raw_event_id(
    payload: Mapping[str, Any],
    message_id: str | None,
) -> str:
    event_type = _optional_string(payload.get("type")) or "UnknownEvent"
    if message_id:
        return f"{DEFAULT_RADAR_VENDOR}:{event_type}:{message_id}"
    return f"{DEFAULT_RADAR_VENDOR}:{event_type}:{uuid.uuid4().hex}"


def _model_to_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_datetime(value)
    return str(value)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _ensure_sqlite_column(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def _optional_env(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _bool_env(values: Mapping[str, str], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise PerceptorWebhookConfigurationError(f"{key} must be a boolean value.")
