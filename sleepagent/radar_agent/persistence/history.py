"""Read-only decoding for records created by retired Agent runtimes.

This module is deliberately narrower than a runtime compatibility layer.  It
can inspect persisted JSON written by the retired fixed and goal-driven
runtimes, but it has no task creation, execution, resume, replanning, worker,
tool, or lease surface.  The historical database tables remain owned by their
already-applied migrations.

Historical payloads are treated as opaque JSON objects.  This preserves fields
added by old deployments without carrying their executable DTO validators,
Agent identities, or runtime methods into the canonical Product runtime.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)

class HistoricalRuntimeReadError(ValueError):
    """Raised when a row cannot be treated as a retired-runtime record."""


@dataclass(frozen=True, slots=True)
class HistoricalRecord(Mapping[str, FrozenJsonValue]):
    """An extra-preserving, recursively immutable historical JSON object."""

    _values: Mapping[str, FrozenJsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_values", _freeze_object(self._values))

    @classmethod
    def decode(
        cls,
        value: Any,
        *,
        record_name: str,
        overlays: Mapping[str, JsonScalar] | None = None,
    ) -> "HistoricalRecord":
        payload = _decode_json_object(value, record_name=record_name)
        for key, expected in (overlays or {}).items():
            existing = payload.get(key)
            if existing is not None and existing != expected:
                raise HistoricalRuntimeReadError(
                    f"{record_name} field {key!r} conflicts with its database column"
                )
            payload.setdefault(key, expected)
        return cls(payload)

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible copy for API/trace serialization."""

        return cast(dict[str, Any], _thaw(self._values))

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        """Small serialization bridge for existing read-only trace consumers."""

        if mode not in {"python", "json"}:
            raise ValueError(f"unsupported historical serialization mode: {mode}")
        return self.to_dict()


class HistoricalTaskRecord(BaseModel):
    """Shared, read-only task fields plus any deployment-specific JSON extras."""

    model_config = ConfigDict(extra="allow", frozen=True)

    task_id: str
    trace_id: str
    subject_id: str
    radar_device_id: str
    role: str
    requested_by_user_id: str | None = None
    role_binding_ids: tuple[str, ...] = ()
    authorization_id: str | None = None
    scenario: str
    provider_input: Mapping[str, Any] = Field(default_factory=dict)
    runtime_kind: str
    runtime_contract_version: str
    execution_mode: str | None = None
    completion_status: str | None = None
    goal_payload: Mapping[str, Any] | None = None
    current_plan_id: str | None = None
    pending_user_input_request_id: str | None = None
    task_version: int = 1
    status: str
    node_status: Mapping[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 0
    idempotency_key: str | None = None
    failure: Mapping[str, Any] | None = None
    parent_task_id: str | None = None
    last_event_sequence: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def decode(
        cls,
        value: Any,
        *,
        overlays: Mapping[str, JsonScalar],
    ) -> "HistoricalTaskRecord":
        payload = _decode_json_object(value, record_name="historical task")
        for key, expected in overlays.items():
            existing = payload.get(key)
            if existing is not None and existing != expected:
                raise HistoricalRuntimeReadError(
                    f"historical task field {key!r} conflicts with its database column"
                )
            payload.setdefault(key, expected)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def freeze_nested_json(self) -> "HistoricalTaskRecord":
        for name in ("provider_input", "node_status"):
            object.__setattr__(self, name, _freeze_object(getattr(self, name)))
        for name in ("goal_payload", "failure"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _freeze_object(value))
        extras = self.__pydantic_extra__ or {}
        object.__setattr__(
            self,
            "__pydantic_extra__",
            MappingProxyType({key: _freeze(value) for key, value in extras.items()}),
        )
        return self

    @model_serializer(mode="plain")
    def serialize_record(self) -> dict[str, Any]:
        values = {
            name: getattr(self, name)
            for name in self.__class__.model_fields
        }
        values.update(self.__pydantic_extra__ or {})
        return cast(dict[str, Any], _thaw(_freeze_object(values)))

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class HistoricalRuntimeTrace:
    """Complete supported read view for one retired-runtime task."""

    task: HistoricalTaskRecord
    events: tuple[HistoricalRecord, ...] = ()
    confirmations: tuple[HistoricalRecord, ...] = ()
    artifacts: tuple[HistoricalRecord, ...] = ()
    a2a_messages: tuple[HistoricalRecord, ...] = ()
    conflicts: tuple[HistoricalRecord, ...] = ()
    plans: tuple[HistoricalRecord, ...] = ()
    model_invocations: tuple[HistoricalRecord, ...] = ()
    agent_invocations: tuple[HistoricalRecord, ...] = ()
    tool_invocations: tuple[HistoricalRecord, ...] = ()
    user_input_requests: tuple[HistoricalRecord, ...] = ()
    budget: HistoricalRecord | None = None
    completion_receipt: HistoricalRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "events": [item.to_dict() for item in self.events],
            "confirmations": [item.to_dict() for item in self.confirmations],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "a2a_messages": [item.to_dict() for item in self.a2a_messages],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "plans": [item.to_dict() for item in self.plans],
            "model_invocations": [
                item.to_dict() for item in self.model_invocations
            ],
            "agent_invocations": [
                item.to_dict() for item in self.agent_invocations
            ],
            "tool_invocations": [item.to_dict() for item in self.tool_invocations],
            "user_input_requests": [
                item.to_dict() for item in self.user_input_requests
            ],
            "budget": self.budget.to_dict() if self.budget is not None else None,
            "completion_receipt": (
                self.completion_receipt.to_dict()
                if self.completion_receipt is not None
                else None
            ),
        }

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        if mode not in {"python", "json"}:
            raise ValueError(f"unsupported historical serialization mode: {mode}")
        return self.to_dict()


class HistoricalRuntimeReader:
    """SELECT-only reader for persisted retired-runtime task history."""

    __slots__ = ("connection", "dialect", "canonical_runtime_kind", "_lock")

    def __init__(
        self,
        connection: Any,
        *,
        dialect: str = "postgres",
        canonical_runtime_kind: str = "product_episode",
        lock: Any | None = None,
    ) -> None:
        if dialect not in {"postgres", "sqlite"}:
            raise ValueError(f"unsupported history-reader dialect: {dialect}")
        self.connection = connection
        self.dialect = dialect
        if not canonical_runtime_kind or not canonical_runtime_kind.strip():
            raise ValueError("canonical_runtime_kind must not be empty")
        self.canonical_runtime_kind = canonical_runtime_kind
        self._lock = lock

    def read_task(self, task_id: str) -> HistoricalTaskRecord:
        self._require_identifier(task_id, name="task_id")
        row = self._fetchone(
            """
            SELECT task_id, trace_id, subject_id, radar_device_id, role,
                   scenario, status, idempotency_key, runtime_kind,
                   runtime_contract_version, execution_mode, completion_status,
                   task_version, task_json, created_at, updated_at
            FROM radar_tasks WHERE task_id = ?
            """,
            (task_id,),
        )
        if row is None:
            raise KeyError(f"historical task not found: {task_id}")
        runtime_kind = str(row[8])
        if runtime_kind == self.canonical_runtime_kind:
            raise HistoricalRuntimeReadError(
                "history reader accepts only records from retired runtimes"
            )
        return HistoricalTaskRecord.decode(
            row[13],
            overlays={
                "task_id": str(row[0]),
                "trace_id": str(row[1]),
                "subject_id": str(row[2]),
                "radar_device_id": str(row[3]),
                "role": str(row[4]),
                "scenario": str(row[5]),
                "status": str(row[6]),
                "idempotency_key": (
                    str(row[7]) if row[7] is not None else None
                ),
                "runtime_kind": runtime_kind,
                "runtime_contract_version": str(row[9]),
                "execution_mode": (
                    str(row[10]) if row[10] is not None else None
                ),
                "completion_status": (
                    str(row[11]) if row[11] is not None else None
                ),
                "task_version": int(row[12]),
                "created_at": str(row[14]),
                "updated_at": str(row[15]),
            },
        )

    def read_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        return self._read_many(
            """
            SELECT event_json FROM radar_task_events
            WHERE task_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (task_id, after_sequence),
            record_name="historical task event",
        )

    def read_confirmations(self, task_id: str) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT confirmation_json FROM radar_human_confirmations
            WHERE task_id = ? ORDER BY created_at, confirmation_id
            """,
            (task_id,),
            record_name="historical human confirmation",
        )

    def read_artifacts(self, task_id: str) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT artifact_json FROM radar_task_artifact_versions
            WHERE task_id = ? ORDER BY created_at, artifact_version_id
            """,
            (task_id,),
            record_name="historical task artifact",
        )

    def read_a2a_messages(self, task_id: str) -> tuple[HistoricalRecord, ...]:
        """Read opaque collaboration records without restoring a mailbox."""

        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT message_json FROM radar_a2a_messages
            WHERE task_id = ? ORDER BY created_at, message_id
            """,
            (task_id,),
            record_name="historical collaboration message",
        )

    def read_conflicts(self, task_id: str) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT conflict_json FROM radar_conflict_records
            WHERE task_id = ? ORDER BY decided_at, conflict_id
            """,
            (task_id,),
            record_name="historical conflict decision",
        )

    def read_plans(self, task_id: str) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT plan_json FROM radar_dynamic_plan_revisions
            WHERE task_id = ? ORDER BY revision, plan_id
            """,
            (task_id,),
            record_name="historical execution plan",
        )

    def read_model_invocations(
        self, task_id: str
    ) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT invocation_json FROM radar_model_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            record_name="historical model invocation",
        )

    def read_agent_invocations(
        self, task_id: str
    ) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT invocation_json FROM radar_agent_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            record_name="historical capability invocation",
        )

    def read_tool_invocations(
        self, task_id: str
    ) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT invocation_json FROM radar_tool_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            record_name="historical tool invocation",
        )

    def read_user_input_requests(
        self, task_id: str
    ) -> tuple[HistoricalRecord, ...]:
        self._require_historical_task(task_id)
        return self._read_many(
            """
            SELECT request_json FROM radar_user_input_requests
            WHERE task_id = ? ORDER BY created_at, request_id
            """,
            (task_id,),
            record_name="historical user-input request",
        )

    def read_runtime_budget(self, task_id: str) -> HistoricalRecord | None:
        self._require_historical_task(task_id)
        return self._read_one_optional(
            "SELECT budget_json FROM radar_runtime_budgets WHERE task_id = ?",
            (task_id,),
            record_name="historical runtime budget",
        )

    def read_completion_receipt(self, task_id: str) -> HistoricalRecord | None:
        self._require_historical_task(task_id)
        return self._read_one_optional(
            """
            SELECT receipt_json FROM radar_completion_receipts
            WHERE task_id = ? ORDER BY completed_at DESC LIMIT 1
            """,
            (task_id,),
            record_name="historical completion receipt",
        )

    def read_trace(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> HistoricalRuntimeTrace:
        task = self.read_task(task_id)
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        return HistoricalRuntimeTrace(
            task=task,
            events=self._read_many(
                """
                SELECT event_json FROM radar_task_events
                WHERE task_id = ? AND sequence > ? ORDER BY sequence
                """,
                (task_id, after_sequence),
                record_name="historical task event",
            ),
            confirmations=self._read_many(
                """
                SELECT confirmation_json FROM radar_human_confirmations
                WHERE task_id = ? ORDER BY created_at, confirmation_id
                """,
                (task_id,),
                record_name="historical human confirmation",
            ),
            artifacts=self._read_many(
                """
                SELECT artifact_json FROM radar_task_artifact_versions
                WHERE task_id = ? ORDER BY created_at, artifact_version_id
                """,
                (task_id,),
                record_name="historical task artifact",
            ),
            a2a_messages=self._read_many(
                """
                SELECT message_json FROM radar_a2a_messages
                WHERE task_id = ? ORDER BY created_at, message_id
                """,
                (task_id,),
                record_name="historical collaboration message",
            ),
            conflicts=self._read_many(
                """
                SELECT conflict_json FROM radar_conflict_records
                WHERE task_id = ? ORDER BY decided_at, conflict_id
                """,
                (task_id,),
                record_name="historical conflict decision",
            ),
            plans=self._read_many(
                """
                SELECT plan_json FROM radar_dynamic_plan_revisions
                WHERE task_id = ? ORDER BY revision, plan_id
                """,
                (task_id,),
                record_name="historical execution plan",
            ),
            model_invocations=self._read_many(
                """
                SELECT invocation_json FROM radar_model_invocations
                WHERE task_id = ? ORDER BY started_at, invocation_id
                """,
                (task_id,),
                record_name="historical model invocation",
            ),
            agent_invocations=self._read_many(
                """
                SELECT invocation_json FROM radar_agent_invocations
                WHERE task_id = ? ORDER BY started_at, invocation_id
                """,
                (task_id,),
                record_name="historical capability invocation",
            ),
            tool_invocations=self._read_many(
                """
                SELECT invocation_json FROM radar_tool_invocations
                WHERE task_id = ? ORDER BY started_at, invocation_id
                """,
                (task_id,),
                record_name="historical tool invocation",
            ),
            user_input_requests=self._read_many(
                """
                SELECT request_json FROM radar_user_input_requests
                WHERE task_id = ? ORDER BY created_at, request_id
                """,
                (task_id,),
                record_name="historical user-input request",
            ),
            budget=self._read_one_optional(
                "SELECT budget_json FROM radar_runtime_budgets WHERE task_id = ?",
                (task_id,),
                record_name="historical runtime budget",
            ),
            completion_receipt=self._read_one_optional(
                """
                SELECT receipt_json FROM radar_completion_receipts
                WHERE task_id = ? ORDER BY completed_at DESC LIMIT 1
                """,
                (task_id,),
                record_name="historical completion receipt",
            ),
        )

    def _require_historical_task(self, task_id: str) -> None:
        self.read_task(task_id)

    @staticmethod
    def _require_identifier(value: str, *, name: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")

    def _read_many(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        record_name: str,
    ) -> tuple[HistoricalRecord, ...]:
        return tuple(
            HistoricalRecord.decode(row[0], record_name=record_name)
            for row in self._fetchall(sql, params)
        )

    def _read_one_optional(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        record_name: str,
    ) -> HistoricalRecord | None:
        row = self._fetchone(sql, params)
        return (
            HistoricalRecord.decode(row[0], record_name=record_name)
            if row is not None
            else None
        )

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        guard = self._lock if self._lock is not None else nullcontext()
        with guard:
            return self.connection.execute(self._sql(sql), params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        guard = self._lock if self._lock is not None else nullcontext()
        with guard:
            return list(self.connection.execute(self._sql(sql), params).fetchall())

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql


def _decode_json_object(value: Any, *, record_name: str) -> dict[str, Any]:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        parsed = json.loads(value) if isinstance(value, str) else dict(value)
        if not isinstance(parsed, dict):
            raise TypeError("JSON root is not an object")
        # Normalize driver-returned mappings to detached, JSON-only values.
        return cast(
            dict[str, Any],
            json.loads(json.dumps(parsed, ensure_ascii=False, allow_nan=False)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalRuntimeReadError(
            f"{record_name} is not a valid JSON object"
        ) from exc


def _freeze_object(value: Mapping[str, Any]) -> Mapping[str, FrozenJsonValue]:
    return MappingProxyType(
        {str(key): _freeze(item) for key, item in value.items()}
    )


def _freeze(value: Any) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return _freeze_object(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise HistoricalRuntimeReadError("historical payload contains a non-JSON value")


def _thaw(value: FrozenJsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


__all__ = [
    "HistoricalRecord",
    "HistoricalRuntimeReadError",
    "HistoricalRuntimeReader",
    "HistoricalRuntimeTrace",
    "HistoricalTaskRecord",
]
