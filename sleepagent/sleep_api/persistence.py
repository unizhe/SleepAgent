"""Durable API-boundary state layered on the shared sleep-domain database."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from sleepagent.persistence import RadarPersistenceStore
from sleepagent.sleep_api.auth import AssertionReplayStore
from sleepagent.sleep_api.contracts import PublicActorRole
from sleepagent.sleep_domain import (
    DomainEvent,
    DomainNamespace,
    IdempotencyConflictError,
    Operation,
)


UTC = timezone.utc


@dataclass(frozen=True)
class OperationCommand:
    operation_id: str
    namespace: DomainNamespace
    route_template: str
    actor_role: PublicActorRole
    authorization_id: str
    authorization_epoch: int
    request: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class FeedbackRecord:
    feedback_id: str
    operation_id: str
    namespace: DomainNamespace
    subject_id: str
    night_episode_id: str
    actor_id: str
    actor_role: PublicActorRole
    authorization_id: str
    authorization_epoch: int
    provenance_category: str
    event_at: datetime
    received_at: datetime
    source_text: str | None
    structured_answer: Mapping[str, Any] | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": "feedback_record.v1",
                "feedback_id": self.feedback_id,
                "operation_id": self.operation_id,
                "subject_id": self.subject_id,
                "night_episode_id": self.night_episode_id,
                "actor_id": self.actor_id,
                "actor_role": self.actor_role.value,
                "authorization_id": self.authorization_id,
                "authorization_epoch": self.authorization_epoch,
                "provenance_category": self.provenance_category,
                "event_at": _dump_datetime(self.event_at),
                "received_at": _dump_datetime(self.received_at),
                "source_text": self.source_text,
                "structured_answer": self.structured_answer,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class EventCursorSession:
    cursor_id: str
    namespace: DomainNamespace
    consumer_service_id: str
    actor_id: str
    subject_id: str
    actor_role: PublicActorRole
    scope_projection_sha256: str
    authorization_epoch: int
    event_schema_generation: str
    issued_at: datetime
    expires_at: datetime
    last_used_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


@dataclass(frozen=True)
class CursorTombstoneRecord:
    cursor_id: str
    event_id: str
    namespace: DomainNamespace
    subject_id: str
    effective_at: datetime
    reason_code: str


class SleepApiPersistence(AssertionReplayStore):
    def __init__(self, store: RadarPersistenceStore) -> None:
        self.store = store
        self.connection = store.connection
        self.dialect = store.dialect

    def consume(
        self,
        *,
        issuer: str,
        assertion_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        with self._transaction(immediate=True) as cursor:
            cursor.execute(
                self._sql(
                    "DELETE FROM sleep_api_actor_assertion_replays "
                    "WHERE expires_at < ?"
                ),
                (_dump_datetime(now),),
            )
            inserted = cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_api_actor_assertion_replays (
                      issuer, assertion_id, nonce, expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """
                ),
                (
                    issuer,
                    assertion_id,
                    nonce,
                    _dump_datetime(expires_at),
                    _dump_datetime(now),
                ),
            ).rowcount
        return inserted == 1

    def create_command_operation(
        self,
        operation: Operation,
        command: OperationCommand,
    ) -> tuple[Operation, bool]:
        if command.operation_id != operation.operation_id:
            raise ValueError("command and operation ids differ")
        if command.namespace.data_mode != operation.data_mode:
            raise ValueError("command and operation data modes differ")
        target_key = operation.target_resource_id or ""
        with self._transaction(immediate=True) as cursor:
            existing = self._find_idempotent_operation(
                cursor,
                namespace=command.namespace,
                operation=operation,
                target_key=target_key,
            )
            if existing is not None:
                return existing, False
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
                    command.namespace.namespace_id,
                    command.namespace.data_mode.value,
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
                    None,
                    0,
                    operation.model_dump_json(),
                    _dump_datetime(operation.created_at),
                    _dump_datetime(operation.updated_at),
                ),
            ).rowcount
            if inserted != 1:
                concurrent = self._find_idempotent_operation(
                    cursor,
                    namespace=command.namespace,
                    operation=operation,
                    target_key=target_key,
                )
                if concurrent is None:
                    raise RuntimeError("idempotent operation was not readable")
                return concurrent, False
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_api_operation_commands (
                      operation_id, namespace_id, data_mode, route_template,
                      actor_role, authorization_id, authorization_epoch,
                      request_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    command.operation_id,
                    command.namespace.namespace_id,
                    command.namespace.data_mode.value,
                    command.route_template,
                    command.actor_role.value,
                    command.authorization_id,
                    command.authorization_epoch,
                    json.dumps(
                        command.request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _dump_datetime(command.created_at),
                ),
            )
        return operation, True

    def get_command(
        self,
        namespace: DomainNamespace,
        *,
        operation_id: str,
    ) -> OperationCommand | None:
        with self.store.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT route_template, actor_role, authorization_id,
                           authorization_epoch, request_json, created_at
                    FROM sleep_api_operation_commands
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
        if row is None:
            return None
        request_json = row[4]
        if not isinstance(request_json, str):
            request_json = json.dumps(request_json)
        return OperationCommand(
            operation_id=operation_id,
            namespace=namespace,
            route_template=str(row[0]),
            actor_role=PublicActorRole(str(row[1])),
            authorization_id=str(row[2]),
            authorization_epoch=int(row[3]),
            request=json.loads(request_json),
            created_at=_parse_datetime(row[5]),
        )

    def save_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT feedback_json
                    FROM sleep_api_feedback
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ?
                    """
                ),
                (
                    feedback.operation_id,
                    feedback.namespace.namespace_id,
                    feedback.namespace.data_mode.value,
                ),
            ).fetchone()
            if row is not None:
                existing = row[0] if isinstance(row[0], str) else json.dumps(row[0])
                if json.loads(existing) != json.loads(feedback.to_json()):
                    raise ValueError("feedback operation is immutable")
                return feedback
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_api_feedback (
                      feedback_id, operation_id, namespace_id, data_mode,
                      subject_id, night_episode_id, night_episode_revision_id,
                      actor_id, actor_role, authorization_id,
                      authorization_epoch, provenance_category, event_at,
                      received_at, feedback_json
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    feedback.feedback_id,
                    feedback.operation_id,
                    feedback.namespace.namespace_id,
                    feedback.namespace.data_mode.value,
                    feedback.subject_id,
                    feedback.night_episode_id,
                    feedback.actor_id,
                    feedback.actor_role.value,
                    feedback.authorization_id,
                    feedback.authorization_epoch,
                    feedback.provenance_category,
                    _dump_datetime(feedback.event_at),
                    _dump_datetime(feedback.received_at),
                    feedback.to_json(),
                ),
            )
        return feedback

    def link_feedback_revision(
        self,
        namespace: DomainNamespace,
        *,
        operation_id: str,
        night_episode_revision_id: str,
    ) -> None:
        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                self._sql(
                    """
                    SELECT night_episode_revision_id
                    FROM sleep_api_feedback
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
            if row is None:
                raise KeyError("feedback record not found")
            if row[0] is not None and str(row[0]) != night_episode_revision_id:
                raise ValueError("feedback revision link is immutable")
            cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_api_feedback
                    SET night_episode_revision_id = ?
                    WHERE operation_id = ? AND namespace_id = ?
                      AND data_mode = ? AND night_episode_revision_id IS NULL
                    """
                ),
                (
                    night_episode_revision_id,
                    operation_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            )

    def count_feedback(self, namespace: DomainNamespace) -> int:
        with self.store.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT COUNT(*)
                    FROM sleep_api_feedback
                    WHERE namespace_id = ? AND data_mode = ?
                    """
                ),
                (namespace.namespace_id, namespace.data_mode.value),
            ).fetchone()
        return int(row[0])

    def create_event_cursor_session(self, session: EventCursorSession) -> None:
        with self._transaction(immediate=True) as cursor:
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_api_event_cursor_sessions (
                      cursor_id, namespace_id, data_mode, consumer_service_id,
                      actor_id, subject_id, actor_role,
                      scope_projection_sha256, authorization_epoch,
                      event_schema_generation, issued_at, expires_at,
                      last_used_at, revoked_at, revocation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    session.cursor_id,
                    session.namespace.namespace_id,
                    session.namespace.data_mode.value,
                    session.consumer_service_id,
                    session.actor_id,
                    session.subject_id,
                    session.actor_role.value,
                    session.scope_projection_sha256,
                    session.authorization_epoch,
                    session.event_schema_generation,
                    _dump_datetime(session.issued_at),
                    _dump_datetime(session.expires_at),
                    _dump_datetime(session.last_used_at),
                    None
                    if session.revoked_at is None
                    else _dump_datetime(session.revoked_at),
                    session.revocation_reason,
                ),
            )

    def get_event_cursor_session(
        self,
        namespace: DomainNamespace,
        *,
        cursor_id: str,
    ) -> EventCursorSession | None:
        with self.store.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT consumer_service_id, actor_id, subject_id, actor_role,
                           scope_projection_sha256, authorization_epoch,
                           event_schema_generation, issued_at, expires_at,
                           last_used_at, revoked_at, revocation_reason
                    FROM sleep_api_event_cursor_sessions
                    WHERE cursor_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    cursor_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
        if row is None:
            return None
        return EventCursorSession(
            cursor_id=cursor_id,
            namespace=namespace,
            consumer_service_id=str(row[0]),
            actor_id=str(row[1]),
            subject_id=str(row[2]),
            actor_role=PublicActorRole(str(row[3])),
            scope_projection_sha256=str(row[4]),
            authorization_epoch=int(row[5]),
            event_schema_generation=str(row[6]),
            issued_at=_parse_datetime(row[7]),
            expires_at=_parse_datetime(row[8]),
            last_used_at=_parse_datetime(row[9]),
            revoked_at=None if row[10] is None else _parse_datetime(row[10]),
            revocation_reason=None if row[11] is None else str(row[11]),
        )

    def touch_event_cursor_session(
        self,
        namespace: DomainNamespace,
        *,
        cursor_id: str,
        used_at: datetime,
    ) -> None:
        with self._transaction(immediate=True) as cursor:
            cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_api_event_cursor_sessions
                    SET last_used_at = ?
                    WHERE cursor_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    _dump_datetime(used_at),
                    cursor_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            )

    def revoke_event_cursor(
        self,
        namespace: DomainNamespace,
        *,
        cursor_id: str,
        event_id: str,
        subject_id: str,
        revoked_at: datetime,
        reason_code: str,
    ) -> CursorTombstoneRecord:
        with self._transaction(immediate=True) as cursor:
            cursor.execute(
                self._sql(
                    """
                    UPDATE sleep_api_event_cursor_sessions
                    SET revoked_at = COALESCE(revoked_at, ?),
                        revocation_reason = COALESCE(revocation_reason, ?)
                    WHERE cursor_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    _dump_datetime(revoked_at),
                    reason_code,
                    cursor_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            )
            cursor.execute(
                self._sql(
                    """
                    INSERT INTO sleep_api_event_cursor_tombstones (
                      cursor_id, event_id, namespace_id, data_mode, subject_id,
                      effective_at, reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (cursor_id) DO NOTHING
                    """
                ),
                (
                    cursor_id,
                    event_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    subject_id,
                    _dump_datetime(revoked_at),
                    reason_code,
                ),
            )
            row = cursor.execute(
                self._sql(
                    """
                    SELECT event_id, subject_id, effective_at, reason_code
                    FROM sleep_api_event_cursor_tombstones
                    WHERE cursor_id = ?
                    """
                ),
                (cursor_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("cursor tombstone was not persisted")
        return CursorTombstoneRecord(
            cursor_id=cursor_id,
            event_id=str(row[0]),
            namespace=namespace,
            subject_id=str(row[1]),
            effective_at=_parse_datetime(row[2]),
            reason_code=str(row[3]),
        )

    def get_event_cursor_tombstone(
        self,
        namespace: DomainNamespace,
        *,
        cursor_id: str,
    ) -> CursorTombstoneRecord | None:
        with self.store.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT event_id, subject_id, effective_at, reason_code
                    FROM sleep_api_event_cursor_tombstones
                    WHERE cursor_id = ? AND namespace_id = ? AND data_mode = ?
                    """
                ),
                (
                    cursor_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
        if row is None:
            return None
        return CursorTombstoneRecord(
            cursor_id=cursor_id,
            event_id=str(row[0]),
            namespace=namespace,
            subject_id=str(row[1]),
            effective_at=_parse_datetime(row[2]),
            reason_code=str(row[3]),
        )

    def list_domain_events_after(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        delivery_offset: int,
        limit: int,
    ) -> tuple[DomainEvent, ...]:
        with self.store.transaction_lock:
            rows = self.connection.execute(
                self._sql(
                    """
                    SELECT event_json
                    FROM sleep_domain_domain_outbox
                    WHERE namespace_id = ? AND data_mode = ?
                      AND subject_id = ? AND delivery_offset > ?
                    ORDER BY delivery_offset ASC
                    LIMIT ?
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    subject_id,
                    delivery_offset,
                    limit,
                ),
            ).fetchall()
        return tuple(
            DomainEvent.model_validate_json(
                row[0] if isinstance(row[0], str) else json.dumps(row[0])
            )
            for row in rows
        )

    def has_domain_events_after(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        delivery_offset: int,
    ) -> bool:
        with self.store.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT 1
                    FROM sleep_domain_domain_outbox
                    WHERE namespace_id = ? AND data_mode = ?
                      AND subject_id = ? AND delivery_offset > ?
                    LIMIT 1
                    """
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    subject_id,
                    delivery_offset,
                ),
            ).fetchone()
        return row is not None

    def _find_idempotent_operation(
        self,
        cursor: Any,
        *,
        namespace: DomainNamespace,
        operation: Operation,
        target_key: str,
    ) -> Operation | None:
        row = cursor.execute(
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
        if row is None:
            return None
        if str(row[0]) != operation.request_sha256:
            raise IdempotencyConflictError(
                "operation idempotency key has a different request hash"
            )
        operation_json = row[1] if isinstance(row[1], str) else json.dumps(row[1])
        return Operation.model_validate_json(operation_json)

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

    def _sql(self, sql: str) -> str:
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")


def _dump_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persistence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value)).astimezone(UTC)


__all__ = [
    "CursorTombstoneRecord",
    "EventCursorSession",
    "FeedbackRecord",
    "OperationCommand",
    "SleepApiPersistence",
]
