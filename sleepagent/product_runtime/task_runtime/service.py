from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from sleepagent.persistence.models import (
    RadarAuditLogEntry,
    RadarDataAuthorization,
    RadarUserRoleBinding,
)
from sleepagent.persistence.store import RadarPersistenceStore
from sleepagent.product_runtime.schemas import HumanConfirmationRequest

from .contracts import (
    CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION,
    CANONICAL_AGENT_RUNTIME_KIND,
    RadarAgentTask,
    RadarArtifactVersion,
    RadarTaskEvent,
    RadarTaskFailure,
    RadarTaskStatus,
)


class InvalidTaskTransition(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class BindingMismatch(ValueError):
    pass


class AuthorizationRequired(PermissionError):
    pass


class RoleAccessDenied(PermissionError):
    pass


_TASK_TRANSITIONS: dict[RadarTaskStatus, set[RadarTaskStatus]] = {
    RadarTaskStatus.CREATED: {RadarTaskStatus.RUNNING, RadarTaskStatus.CANCELLED},
    RadarTaskStatus.RUNNING: {
        RadarTaskStatus.WAITING_FOR_USER_INPUT,
        RadarTaskStatus.WAITING_FOR_CONFIRMATION,
        RadarTaskStatus.COMPLETED,
        RadarTaskStatus.FAILED,
        RadarTaskStatus.CANCELLED,
    },
    RadarTaskStatus.WAITING_FOR_USER_INPUT: {
        RadarTaskStatus.RUNNING,
        RadarTaskStatus.FAILED,
        RadarTaskStatus.CANCELLED,
    },
    RadarTaskStatus.WAITING_FOR_CONFIRMATION: {
        RadarTaskStatus.RUNNING,
        RadarTaskStatus.FAILED,
        RadarTaskStatus.CANCELLED,
    },
    RadarTaskStatus.FAILED: {RadarTaskStatus.RUNNING},
    RadarTaskStatus.COMPLETED: set(),
    RadarTaskStatus.CANCELLED: set(),
}


class TaskService:
    """Persistent lifecycle owner for canonical Product Episode tasks."""

    def __init__(
        self,
        store: RadarPersistenceStore,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        validate_bindings: bool = True,
        require_authorization: bool = True,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._validate_bindings_enabled = validate_bindings
        self._require_authorization = require_authorization
        self._lock = RLock()

    def create_task(
        self,
        *,
        subject_id: str,
        radar_device_id: str,
        role: str = "family",
        role_binding_ids: list[str] | None = None,
        actor_id: str | None = None,
        authorization_id: str | None = None,
        scenario: str = "replay",
        provider_input: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        max_retries: int = 3,
        parent_task_id: str | None = None,
        goal_payload: dict[str, Any] | None = None,
    ) -> RadarAgentTask:
        with self._lock:
            requested_bindings = list(role_binding_ids or [])
            requested_input = dict(provider_input or {})
            if idempotency_key:
                existing = self.store.get_task_by_idempotency_key(idempotency_key)
                if existing is not None:
                    fingerprint = (
                        subject_id,
                        radar_device_id,
                        role,
                        actor_id,
                        requested_bindings,
                        authorization_id,
                        scenario,
                        requested_input,
                        CANONICAL_AGENT_RUNTIME_KIND,
                        CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION,
                        goal_payload,
                    )
                    existing_fingerprint = (
                        existing.subject_id,
                        existing.radar_device_id,
                        existing.role,
                        existing.requested_by_user_id,
                        existing.role_binding_ids,
                        existing.authorization_id,
                        existing.scenario,
                        existing.provider_input,
                        existing.runtime_kind,
                        existing.runtime_contract_version,
                        existing.goal_payload,
                    )
                    if fingerprint != existing_fingerprint:
                        raise IdempotencyConflict(
                            "idempotency key is already bound to a different task request"
                        )
                    return existing

            if self._require_authorization:
                if not actor_id or not authorization_id:
                    raise AuthorizationRequired(
                        "actor_id and active data authorization are required"
                    )
                self._require_data_authorization(
                    subject_id=subject_id,
                    authorization_id=authorization_id,
                    required_scopes={"process_radar_summary"},
                )
                self._require_actor_permission(
                    subject_id=subject_id,
                    actor_id=actor_id,
                    role_binding_ids=requested_bindings,
                    allowed_roles={"elder", "family", "doctor"},
                    permission="process_health_data",
                )
            if self._validate_bindings_enabled:
                self._validate_bindings(
                    subject_id=subject_id,
                    radar_device_id=radar_device_id,
                    role=role,
                    role_binding_ids=requested_bindings,
                )

            now = self._clock()
            task = RadarAgentTask(
                task_id=f"task-{self._id_factory()}",
                trace_id=f"trace-{self._id_factory()}",
                subject_id=subject_id,
                radar_device_id=radar_device_id,
                role=role,
                requested_by_user_id=actor_id,
                role_binding_ids=requested_bindings,
                authorization_id=authorization_id,
                scenario=scenario,
                provider_input=requested_input,
                goal_payload=goal_payload,
                max_retries=max_retries,
                idempotency_key=idempotency_key,
                parent_task_id=parent_task_id,
                created_at=now,
                updated_at=now,
            )
            self.store.save_task(task)
            task = self._record_event(
                task,
                "task.created",
                "Task created.",
                {"subject_id": subject_id, "radar_device_id": radar_device_id, "role": role},
            )
            self._audit(task, "task_created", task.task_id, "Task lifecycle created.")
            return task

    def assert_task_access(
        self,
        task_id: str,
        *,
        actor_id: str,
        actor_role: str,
        permission: str = "process_health_data",
    ) -> RadarAgentTask:
        """Revalidate persisted subject/device/role/authorization bindings."""

        task = self.get_task(task_id)
        if task.requested_by_user_id != actor_id or task.role != actor_role:
            raise RoleAccessDenied("actor identity does not match the task binding")
        if not task.authorization_id:
            raise AuthorizationRequired("task has no active data authorization")
        self._validate_bindings(
            subject_id=task.subject_id,
            radar_device_id=task.radar_device_id,
            role=task.role,
            role_binding_ids=list(task.role_binding_ids),
        )
        self._require_data_authorization(
            subject_id=task.subject_id,
            authorization_id=task.authorization_id,
            required_scopes={"process_radar_summary"},
        )
        self._require_actor_permission(
            subject_id=task.subject_id,
            actor_id=actor_id,
            role_binding_ids=list(task.role_binding_ids),
            allowed_roles={actor_role},
            permission=permission,
        )
        return task

    def grant_data_authorization(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role_binding_id: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> RadarDataAuthorization:
        binding = self._require_actor_permission(
            subject_id=subject_id,
            actor_id=actor_id,
            role_binding_ids=[role_binding_id],
            allowed_roles={"elder", "family"},
            permission="manage_authorization",
        )
        authorization = RadarDataAuthorization(
            authorization_id=f"authorization-{self._id_factory()}",
            subject_id=subject_id,
            granted_by_user_id=actor_id,
            granted_by_role=binding.role,
            scopes=scopes,
            granted_at=self._clock(),
            expires_at=expires_at,
        )
        self.store.save_data_authorization(authorization)
        self.record_audit(
            task_id=None,
            actor=_actor_fingerprint(actor_id),
            action="data_authorization_granted",
            target_ref=authorization.authorization_id,
            summary="Data processing scopes granted.",
            payload={"subject_ref": _subject_fingerprint(subject_id), "scopes": scopes},
        )
        return authorization

    def revoke_data_authorization(
        self,
        authorization_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
    ) -> RadarDataAuthorization:
        authorization = self.store.get_data_authorization(authorization_id)
        self._require_actor_permission(
            subject_id=authorization.subject_id,
            actor_id=actor_id,
            role_binding_ids=[role_binding_id],
            allowed_roles={"elder", "family"},
            permission="manage_authorization",
        )
        if authorization.status == "revoked":
            if authorization.revoked_by == actor_id:
                return authorization
            raise RoleAccessDenied("authorization was already revoked by another actor")
        revoked = authorization.model_copy(
            update={
                "status": "revoked",
                "revoked_at": self._clock(),
                "revoked_by": actor_id,
            }
        )
        self.store.save_data_authorization(revoked)
        self.record_audit(
            task_id=None,
            actor=_actor_fingerprint(actor_id),
            action="data_authorization_revoked",
            target_ref=authorization_id,
            summary="Data processing authorization revoked.",
            payload={"subject_ref": _subject_fingerprint(authorization.subject_id)},
        )
        return revoked

    def export_subject_data(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        binding = self._require_actor_permission(
            subject_id=subject_id,
            actor_id=actor_id,
            role_binding_ids=[role_binding_id],
            allowed_roles={"elder", "family"},
            permission="export_data",
        )
        authorization = self._require_data_authorization(
            subject_id=subject_id,
            authorization_id=authorization_id,
            required_scopes={"export_data"},
        )
        subject = self.store.get_subject(subject_id)
        tasks = [task for task in self.store.list_tasks() if task.subject_id == subject_id]
        artifacts = [
            version
            for task in tasks
            for version in self.store.list_task_artifact_versions(task.task_id)
        ]
        payload = {
            "schema_version": "radar-subject-export.v1",
            "subject": subject.model_dump(mode="json"),
            "exported_by_role": binding.role,
            "authorization": {
                "authorization_id": authorization.authorization_id,
                "scopes": list(authorization.scopes),
            },
            "tasks": [
                {
                    "task_id": task.task_id,
                    "trace_id": task.trace_id,
                    "role": task.role,
                    "scenario": task.scenario,
                    "status": _task_export_value(task, "status"),
                    "authorization_id": task.authorization_id,
                    "created_at": _task_export_value(task, "created_at"),
                    "updated_at": _task_export_value(task, "updated_at"),
                }
                for task in tasks
            ],
            "night_summaries": [
                item.model_dump(mode="json")
                for item in self.store.list_night_summaries(subject_id)
            ],
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "memory_summaries": [
                item.model_dump(mode="json")
                for item in self.store.list_memory_summaries(subject_id)
            ],
            "alerts": [
                item.model_dump(mode="json")
                for item in self.store.list_alerts(subject_id)
            ],
            "excluded": [
                "raw_radar_stream",
                "provider_credentials",
                "llm_prompts_and_responses",
                "audit_log_payloads",
            ],
        }
        self.record_audit(
            task_id=None,
            actor=_actor_fingerprint(actor_id),
            action="subject_data_exported",
            target_ref=_subject_fingerprint(subject_id),
            summary="Authorized minimized subject data export created.",
            payload={
                "authorization_id": authorization_id,
                "task_count": len(tasks),
                "artifact_count": len(artifacts),
            },
        )
        return payload

    def delete_subject_data(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        self._require_actor_permission(
            subject_id=subject_id,
            actor_id=actor_id,
            role_binding_ids=[role_binding_id],
            allowed_roles={"elder", "family"},
            permission="delete_data",
        )
        self._require_data_authorization(
            subject_id=subject_id,
            authorization_id=authorization_id,
            required_scopes={"delete_data"},
        )
        subject_ref = _subject_fingerprint(subject_id)
        deleted = self.store.delete_subject_data(subject_id)
        receipt = {
            "schema_version": "radar-subject-deletion.v1",
            "subject_ref": subject_ref,
            "deleted": deleted,
        }
        self.record_audit(
            task_id=None,
            actor=_actor_fingerprint(actor_id),
            action="subject_data_deleted",
            target_ref=subject_ref,
            summary="Authorized subject data deletion completed.",
            payload={"deleted": deleted},
        )
        return receipt

    def get_task(self, task_id: str) -> RadarAgentTask:
        return self.store.get_task(task_id)

    @staticmethod
    def _require_canonical_runtime(task: RadarAgentTask) -> RadarAgentTask:
        return task

    def list_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[RadarTaskEvent]:
        self.get_task(task_id)
        return self.store.list_task_events(task_id, after_sequence=after_sequence)

    def list_artifacts(
        self,
        task_id: str,
        *,
        artifact_id: str | None = None,
    ) -> list[RadarArtifactVersion]:
        """Read task artifact history through the lifecycle boundary."""

        self.get_task(task_id)
        return self.store.list_task_artifact_versions(task_id, artifact_id=artifact_id)

    def list_confirmations(self, task_id: str) -> list[HumanConfirmationRequest]:
        """Read task confirmation history through the lifecycle boundary."""

        self.get_task(task_id)
        return self.store.list_confirmations(task_id)

    def emit_event(
        self,
        task_id: str,
        *,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> RadarTaskEvent:
        with self._lock:
            task = self._require_canonical_runtime(self.get_task(task_id))
            updated = self._record_event(task, event_type, message, payload or {})
            event = self.store.list_task_events(
                task_id, after_sequence=updated.last_event_sequence - 1
            )[0]
            return event

    def append_event(self, task_id: str, event: RadarTaskEvent) -> None:
        """Append a prebuilt event while enforcing task identity and strict order."""
        with self._lock:
            task = self._require_canonical_runtime(self.get_task(task_id))
            next_sequence = self._next_event_sequence(task)
            if (
                event.task_id != task_id
                or event.trace_id != task.trace_id
                or event.sequence != next_sequence
            ):
                raise ValueError("event identity or sequence does not match the task stream")
            self.store.save_task_event(event)
            self.store.save_task(
                task.model_copy(
                    update={
                        "last_event_sequence": event.sequence,
                        "updated_at": self._clock(),
                    }
                )
            )

    def transition_task(
        self,
        task_id: str,
        target: RadarTaskStatus,
        *,
        message: str | None = None,
    ) -> RadarAgentTask:
        with self._lock:
            task = self._require_canonical_runtime(self.get_task(task_id))
            if target not in _TASK_TRANSITIONS[task.status]:
                raise InvalidTaskTransition(f"cannot transition task {task.status.value} -> {target.value}")
            task = task.model_copy(
                update={"status": target, "updated_at": self._clock()}
            )
            self.store.save_task(task)
            task = self._record_event(
                task,
                f"task.{target.value}",
                message or f"Task transitioned to {target.value}.",
                {"status": target.value},
            )
            self._audit(task, "task_status_changed", task.task_id, f"Task is {target.value}.")
            return task

    def fail_task(
        self,
        task_id: str,
        *,
        error_code: str,
        message: str,
        failed_node: str | None = None,
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> RadarAgentTask:
        with self._lock:
            task = self._require_canonical_runtime(self.get_task(task_id))
            if task.status not in {
                RadarTaskStatus.RUNNING,
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
            }:
                raise InvalidTaskTransition("only an active task can fail")
            failure = RadarTaskFailure(
                error_code=error_code,
                message=message,
                failed_node=failed_node,
                retryable=retryable,
                details=details or {},
                occurred_at=self._clock(),
            )
            task = task.model_copy(
                update={
                    "status": RadarTaskStatus.FAILED,
                    "failure": failure,
                    "updated_at": self._clock(),
                }
            )
            self.store.save_task(task)
            task = self._record_event(
                task,
                "task.failed",
                message,
                {"error_code": error_code, "failed_node": failed_node, "retryable": retryable},
            )
            self._audit(task, "task_failed", failed_node or task.task_id, message)
            return task

    def retry_failed_task(self, task_id: str) -> RadarAgentTask:
        with self._lock:
            task = self._require_canonical_runtime(self.get_task(task_id))
            if task.status != RadarTaskStatus.FAILED or task.failure is None:
                raise InvalidTaskTransition("only a failed task can be retried")
            if not task.failure.retryable:
                raise InvalidTaskTransition("task failure is not retryable")
            if task.retry_count >= task.max_retries:
                raise InvalidTaskTransition("task retry limit reached")
            task = task.model_copy(
                update={
                    "status": RadarTaskStatus.RUNNING,
                    "retry_count": task.retry_count + 1,
                    "failure": None,
                    "updated_at": self._clock(),
                }
            )
            self.store.save_task(task)
            task = self._record_event(
                task,
                "task.retried",
                "Failed task scheduled for retry.",
                {"retry_count": task.retry_count},
            )
            self._audit(task, "task_retried", task.task_id, "Failed task retried.")
            return task

    def rerun_failed_task(
        self,
        task_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> RadarAgentTask:
        original = self._require_canonical_runtime(self.get_task(task_id))
        if original.status != RadarTaskStatus.FAILED:
            raise InvalidTaskTransition("only a failed task can be rerun")
        rerun = self.create_task(
            subject_id=original.subject_id,
            radar_device_id=original.radar_device_id,
            role=original.role,
            actor_id=original.requested_by_user_id,
            role_binding_ids=original.role_binding_ids,
            authorization_id=original.authorization_id,
            scenario=original.scenario,
            provider_input=original.provider_input,
            idempotency_key=idempotency_key,
            max_retries=original.max_retries,
            parent_task_id=original.task_id,
            goal_payload=original.goal_payload,
        )
        self._audit(rerun, "failed_task_rerun_created", original.task_id, "Created a fresh rerun task.")
        return rerun

    def recover_incomplete_tasks(self) -> list[RadarAgentTask]:
        recovered: list[RadarAgentTask] = []
        with self._lock:
            tasks = self.store.list_tasks(statuses={RadarTaskStatus.RUNNING})
            for task in tasks:
                task = task.model_copy(
                    update={
                        "status": RadarTaskStatus.CREATED,
                        "updated_at": self._clock(),
                    }
                )
                self.store.save_task(task)
                task = self._record_event(
                    task,
                    "task.recovered",
                    "Interrupted task recovered for a safe restart.",
                    {},
                )
                self._audit(task, "task_recovered", task.task_id, "Recovered interrupted task.")
                recovered.append(task)
        return recovered

    def save_payload_artifact(
        self,
        task_id: str,
        *,
        artifact_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        source_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RadarArtifactVersion:
        """Version non-report artifacts such as exports, documents, and replay checks."""
        task = self._require_canonical_runtime(self.get_task(task_id))
        versions = self.store.list_task_artifact_versions(task_id, artifact_id=artifact_id)
        version = RadarArtifactVersion(
            artifact_version_id=f"artifact-version-{self._id_factory()}",
            artifact_id=artifact_id,
            task_id=task_id,
            artifact_type=artifact_type,
            version=len(versions) + 1,
            payload=payload,
            source_refs=source_refs or [],
            metadata=metadata or {},
            created_at=self._clock(),
        )
        self.store.save_task_artifact_version(version)
        self._record_event(
            task,
            "artifact.version_created",
            f"Created {artifact_type} version {version.version}.",
            {"artifact_id": artifact_id, "version": version.version},
        )
        self._audit(task, "artifact_version_created", artifact_id, f"Created version {version.version}.")
        return version

    def record_audit(
        self,
        task_id: str | None,
        *,
        actor: str,
        action: str,
        target_ref: str | None = None,
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> RadarAuditLogEntry:
        task = self.get_task(task_id) if task_id is not None else None
        if task is not None:
            task = self._require_canonical_runtime(task)
        entry = RadarAuditLogEntry(
            audit_id=f"audit-{self._id_factory()}",
            task_id=task_id,
            actor=actor,
            action=action,
            target_ref=target_ref,
            summary=summary,
            payload={
                **({"trace_id": task.trace_id} if task is not None else {}),
                **(payload or {}),
            },
            created_at=self._clock(),
        )
        return self.store.save_audit_log(entry)

    def _validate_bindings(
        self,
        *,
        subject_id: str,
        radar_device_id: str,
        role: str,
        role_binding_ids: list[str],
    ) -> None:
        self.store.get_subject(subject_id)
        device = self.store.get_device(radar_device_id)
        if device.bound_subject_id != subject_id:
            raise BindingMismatch("radar device is not bound to the requested subject")
        bindings = {binding.role_binding_id: binding for binding in self.store.list_role_bindings(subject_id)}
        for binding_id in role_binding_ids:
            binding = bindings.get(binding_id)
            if binding is None or binding.role != role:
                raise BindingMismatch("role binding does not match the requested subject and role")

    def _require_actor_permission(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role_binding_ids: list[str],
        allowed_roles: set[str],
        permission: str,
    ) -> RadarUserRoleBinding:
        if not role_binding_ids:
            raise RoleAccessDenied("a role binding is required")
        bindings = {
            binding.role_binding_id: binding
            for binding in self.store.list_role_bindings(subject_id)
        }
        for binding_id in role_binding_ids:
            binding = bindings.get(binding_id)
            if (
                binding is not None
                and binding.user_id == actor_id
                and binding.role in allowed_roles
                and (permission in binding.permissions or "*" in binding.permissions)
            ):
                return binding
        raise RoleAccessDenied(
            f"actor lacks {permission!r} permission for this subject"
        )

    def _require_data_authorization(
        self,
        *,
        subject_id: str,
        authorization_id: str,
        required_scopes: set[str],
    ) -> RadarDataAuthorization:
        try:
            authorization = self.store.get_data_authorization(authorization_id)
        except KeyError as exc:
            raise AuthorizationRequired("data authorization was not found") from exc
        if authorization.subject_id != subject_id:
            raise AuthorizationRequired("authorization belongs to another subject")
        if authorization.status != "active":
            raise AuthorizationRequired("data authorization is not active")
        if (
            authorization.expires_at is not None
            and authorization.expires_at <= self._clock()
        ):
            raise AuthorizationRequired("data authorization has expired")
        missing = required_scopes - set(authorization.scopes)
        if missing:
            raise AuthorizationRequired(
                "authorization is missing scopes: " + ", ".join(sorted(missing))
            )
        return authorization

    def _record_event(
        self,
        task: RadarAgentTask,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> RadarAgentTask:
        with self._lock:
            sequence = self._next_event_sequence(task)
            event = RadarTaskEvent(
                event_id=f"event-{self._id_factory()}",
                task_id=task.task_id,
                trace_id=task.trace_id,
                sequence=sequence,
                event_type=event_type,
                message=message,
                payload=payload,
                created_at=self._clock(),
            )
            self.store.save_task_event(event)
            from sleepagent.observability import log_event

            log_event(
                "radar_task_event",
                task_id=task.task_id,
                trace_id=task.trace_id,
                sequence=sequence,
                event_type=event_type,
                message=message,
            )
            updated = task.model_copy(
                update={"last_event_sequence": sequence, "updated_at": self._clock()}
            )
            self.store.save_task(updated)
            return updated

    def _next_event_sequence(self, task: RadarAgentTask) -> int:
        events = self.store.list_task_events(task.task_id)
        if not events:
            persisted_sequence = 0
        else:
            persisted_sequence = events[-1].sequence
        return max(task.last_event_sequence, persisted_sequence) + 1

    def _audit(
        self,
        task: RadarAgentTask,
        action: str,
        target_ref: str,
        summary: str,
    ) -> None:
        self.record_audit(
            task.task_id,
            actor="task_service",
            action=action,
            target_ref=target_ref,
            summary=summary,
        )


def _task_export_value(task: RadarAgentTask, field_name: str) -> Any:
    """Serialize one canonical task field for the export boundary."""

    return task.model_dump(mode="json")[field_name]


def _subject_fingerprint(subject_id: str) -> str:
    digest = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()[:16]
    return f"subject:[hash:{digest}]"


def _actor_fingerprint(actor_id: str) -> str:
    digest = hashlib.sha256(actor_id.encode("utf-8")).hexdigest()[:16]
    return f"actor:[hash:{digest}]"


__all__ = [
    "AuthorizationRequired",
    "BindingMismatch",
    "IdempotencyConflict",
    "InvalidTaskTransition",
    "RoleAccessDenied",
    "TaskService",
]
