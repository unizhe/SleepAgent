from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from sleepagent.radar_agent.agents import ContextPacket
from sleepagent.radar_agent.a2a import A2AEventBus, InMemoryA2AMailbox
from sleepagent.radar_agent.confirmation import canonical_action, confirmation_rule
from sleepagent.radar_agent.orchestrator import (
    WORKFLOW_NODE_ORDER,
    OrchestratorAgent,
    OrchestratorDecision,
)
from sleepagent.radar_agent.persistence.models import (
    RadarAuditLogEntry,
    RadarDataAuthorization,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.memory.contracts import MemoryWriteDecision
from sleepagent.radar_agent.memory.service import MemoryPrivacyFilter
from sleepagent.radar_agent.persistence.store import RadarPersistenceStore
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    ConflictRecord,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentName,
    RoleReportArtifact,
)

from .contracts import (
    RadarAgentTask,
    RadarArtifactVersion,
    RadarNodeStatus,
    RadarTaskEvent,
    RadarTaskFailure,
    RadarTaskStatus,
)


class InvalidTaskTransition(ValueError):
    pass


class InvalidNodeTransition(ValueError):
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

_NODE_TRANSITIONS: dict[RadarNodeStatus, set[RadarNodeStatus]] = {
    RadarNodeStatus.PENDING: {RadarNodeStatus.RUNNING, RadarNodeStatus.SKIPPED},
    RadarNodeStatus.RUNNING: {
        RadarNodeStatus.WAITING_FOR_CONFIRMATION,
        RadarNodeStatus.SUCCEEDED,
        RadarNodeStatus.FAILED,
    },
    RadarNodeStatus.WAITING_FOR_CONFIRMATION: {
        RadarNodeStatus.RUNNING,
        RadarNodeStatus.SUCCEEDED,
        RadarNodeStatus.FAILED,
    },
    RadarNodeStatus.FAILED: {RadarNodeStatus.RUNNING},
    RadarNodeStatus.SUCCEEDED: set(),
    RadarNodeStatus.SKIPPED: set(),
}


class TaskService:
    """Persistent lifecycle owner around the single-task OrchestratorAgent."""

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
        runtime_kind: str = "legacy_fixed",
        runtime_contract_version: str = "radar-legacy.v1",
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
                        runtime_kind,
                        runtime_contract_version,
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
                runtime_kind=runtime_kind,
                runtime_contract_version=runtime_contract_version,
                goal_payload=goal_payload,
                node_status=(
                    {node.value: RadarNodeStatus.PENDING for node in WORKFLOW_NODE_ORDER}
                    if runtime_kind == "legacy_fixed"
                    else {}
                ),
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
            role_binding_ids=task.role_binding_ids,
        )
        self._require_data_authorization(
            subject_id=task.subject_id,
            authorization_id=task.authorization_id,
            required_scopes={"process_radar_summary"},
        )
        self._require_actor_permission(
            subject_id=task.subject_id,
            actor_id=actor_id,
            role_binding_ids=task.role_binding_ids,
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
                    "status": task.status.value,
                    "authorization_id": task.authorization_id,
                    "created_at": task.created_at.isoformat(),
                    "updated_at": task.updated_at.isoformat(),
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

    def list_role_reports(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> list[RadarArtifactVersion]:
        bindings = {
            item.role_binding_id: item
            for item in self.store.list_role_bindings(subject_id)
        }
        binding = bindings.get(role_binding_id)
        if binding is None or binding.user_id != actor_id:
            raise RoleAccessDenied("actor has no role binding for this subject")
        allowed_report_roles = {
            "elder": {"elder"},
            "family": {"elder", "family"},
            "doctor": {"doctor"},
        }.get(binding.role)
        required_permission = (
            "read_doctor_material" if binding.role == "doctor" else "read_reports"
        )
        if (
            allowed_report_roles is None
            or (
                required_permission not in binding.permissions
                and "*" not in binding.permissions
            )
        ):
            raise RoleAccessDenied("actor cannot read role reports")
        self._require_data_authorization(
            subject_id=subject_id,
            authorization_id=authorization_id,
            required_scopes={"process_radar_summary"},
        )
        tasks = [task for task in self.store.list_tasks() if task.subject_id == subject_id]
        return [
            version
            for task in tasks
            for version in self.store.list_task_artifact_versions(task.task_id)
            if version.report is not None
            and version.report.role in allowed_report_roles
        ]

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

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[RadarTaskEvent]:
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

    def list_a2a_messages(self, task_id: str) -> list[A2AMessage]:
        """Read minimized A2A history through the lifecycle boundary."""

        self.get_task(task_id)
        return self.store.list_a2a_messages(task_id)

    def list_conflicts(self, task_id: str) -> list[ConflictRecord]:
        """Read Orchestrator conflict decisions through the lifecycle boundary."""

        self.get_task(task_id)
        return self.store.list_conflicts(task_id)

    def emit_event(
        self,
        task_id: str,
        *,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> RadarTaskEvent:
        with self._lock:
            task = self.get_task(task_id)
            updated = self._record_event(task, event_type, message, payload or {})
            return self.store.list_task_events(
                task_id, after_sequence=updated.last_event_sequence - 1
            )[0]

    def append_event(self, task_id: str, event: RadarTaskEvent) -> None:
        """Append a prebuilt event while enforcing task identity and strict order."""
        with self._lock:
            task = self.get_task(task_id)
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

    def forward_a2a_message(
        self,
        task_id: str,
        message: A2AMessage,
        *,
        approved_by_orchestrator: bool = True,
    ) -> A2AMessage:
        with self._lock:
            task = self.get_task(task_id)
            if message.task_id != task_id:
                raise ValueError("A2A message task_id does not match lifecycle task")
            bus = A2AEventBus(InMemoryA2AMailbox(store=self.store))
            forwarded = bus.forward_from_orchestrator(
                message,
                approved_by_orchestrator=approved_by_orchestrator,
            )
            self._record_event(
                task,
                "a2a.message_forwarded",
                f"Forwarded A2A message {message.message_id}.",
                {
                    "message_id": message.message_id,
                    "sender": message.sender,
                    "receiver": message.receiver,
                    "collaboration_round": message.collaboration_round,
                },
            )
            self._audit(
                task,
                "a2a_message_forwarded",
                message.message_id,
                f"Forwarded {message.intent}.",
            )
            return forwarded

    def approve_cross_task_share(
        self,
        source_task_id: str,
        target_task_id: str,
        *,
        sender: str,
        receiver: str,
        intent: str,
        shared_artifact_type: str,
        payload: dict[str, Any],
        evidence_refs: list[str] | None = None,
        approved_by_orchestrator: bool,
        collaboration_round: int = 1,
    ) -> A2AMessage:
        with self._lock:
            source = self.get_task(source_task_id)
            self.get_task(target_task_id)
            message = A2AMessage(
                message_id=f"a2a-{self._id_factory()}",
                task_id=source_task_id,
                target_task_id=target_task_id,
                sender=sender,
                receiver=receiver,
                intent=intent,
                evidence_refs=evidence_refs or [],
                requires_approval=True,
                collaboration_round=collaboration_round,
                shared_artifact_type=shared_artifact_type,
                payload=payload,
                created_at=self._clock(),
            )
            bus = A2AEventBus(InMemoryA2AMailbox(store=self.store))
            forwarded = bus.forward_from_orchestrator(
                message,
                approved_by_orchestrator=approved_by_orchestrator,
                approved_by_workflow_runtime=True,
            )
            self._record_event(
                source,
                "a2a.cross_task_share_approved",
                f"Approved cross-task A2A share to {target_task_id}.",
                {
                    "message_id": forwarded.message_id,
                    "target_task_id": target_task_id,
                    "shared_artifact_type": shared_artifact_type,
                },
            )
            self._audit(
                source,
                "a2a_cross_task_share_approved",
                forwarded.message_id,
                f"Approved {shared_artifact_type} share to {target_task_id}.",
            )
            return forwarded

    def transition_task(
        self,
        task_id: str,
        target: RadarTaskStatus,
        *,
        message: str | None = None,
    ) -> RadarAgentTask:
        with self._lock:
            task = self.get_task(task_id)
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

    def transition_node(
        self,
        task_id: str,
        node: str,
        target: RadarNodeStatus,
    ) -> RadarAgentTask:
        with self._lock:
            task = self.get_task(task_id)
            if task.status not in {
                RadarTaskStatus.RUNNING,
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
            }:
                raise InvalidNodeTransition("nodes can only advance in an active task")
            current = task.node_status.get(node, RadarNodeStatus.PENDING)
            if target not in _NODE_TRANSITIONS[current]:
                raise InvalidNodeTransition(
                    f"cannot transition node {node} {current.value} -> {target.value}"
                )
            statuses = dict(task.node_status)
            statuses[node] = target
            task = task.model_copy(
                update={"node_status": statuses, "updated_at": self._clock()}
            )
            self.store.save_task(task)
            task = self._record_event(
                task,
                f"node.{target.value}",
                f"Node {node} transitioned to {target.value}.",
                {"node": node, "status": target.value},
            )
            self._audit(task, "node_status_changed", node, f"Node is {target.value}.")
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
            task = self.get_task(task_id)
            if task.status not in {
                RadarTaskStatus.RUNNING,
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
            }:
                raise InvalidTaskTransition("only an active task can fail")
            node_status = dict(task.node_status)
            if failed_node is not None:
                current = node_status.get(failed_node, RadarNodeStatus.PENDING)
                if current == RadarNodeStatus.RUNNING:
                    node_status[failed_node] = RadarNodeStatus.FAILED
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
                    "node_status": node_status,
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
            task = self.get_task(task_id)
            if task.status != RadarTaskStatus.FAILED or task.failure is None:
                raise InvalidTaskTransition("only a failed task can be retried")
            if not task.failure.retryable:
                raise InvalidTaskTransition("task failure is not retryable")
            if task.retry_count >= task.max_retries:
                raise InvalidTaskTransition("task retry limit reached")
            statuses = dict(task.node_status)
            for node, status in statuses.items():
                if status == RadarNodeStatus.FAILED:
                    statuses[node] = RadarNodeStatus.PENDING
            task = task.model_copy(
                update={
                    "status": RadarTaskStatus.RUNNING,
                    "node_status": statuses,
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
        original = self.get_task(task_id)
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
        )
        self._audit(rerun, "failed_task_rerun_created", original.task_id, "Created a fresh rerun task.")
        return rerun

    def recover_incomplete_tasks(self) -> list[RadarAgentTask]:
        recovered: list[RadarAgentTask] = []
        with self._lock:
            tasks = self.store.list_tasks(statuses={RadarTaskStatus.RUNNING})
            for task in tasks:
                statuses = {
                    node: (RadarNodeStatus.PENDING if status == RadarNodeStatus.RUNNING else status)
                    for node, status in task.node_status.items()
                }
                task = task.model_copy(
                    update={
                        "status": RadarTaskStatus.CREATED,
                        "node_status": statuses,
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

    def save_artifact(
        self,
        task_id: str,
        artifact: RoleReportArtifact | EvidenceLedger,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RadarArtifactVersion:
        task = self.get_task(task_id)
        if artifact.task_id != task_id:
            raise ValueError("artifact task_id does not match lifecycle task")
        if isinstance(artifact, RoleReportArtifact):
            artifact_id = artifact.artifact_id
            artifact_type = f"role_report:{artifact.role}"
            report = artifact
            ledger = None
            source_refs = [*artifact.claim_ids, *artifact.evidence_refs]
            resolved_metadata = {
                "source_ledger_id": artifact.source_ledger_id,
                "risk_level": artifact.risk_level.value,
                "prompt_version": artifact.prompt_version,
                "model_provider": artifact.model_provider,
                "model_id": artifact.model_id,
                "generation_mode": artifact.generation_mode,
                **(metadata or {}),
            }
        else:
            artifact_id = artifact.ledger_id
            artifact_type = "evidence_ledger"
            report = None
            ledger = artifact
            source_refs = [claim.claim_id for claim in artifact.claims]
            resolved_metadata = metadata or {}
        versions = self.store.list_task_artifact_versions(task_id, artifact_id=artifact_id)
        if versions:
            latest = max(versions, key=lambda item: item.version)
            same_payload = (
                latest.report == report
                if report is not None
                else latest.evidence_ledger == ledger
            )
            if same_payload and latest.metadata == resolved_metadata:
                return latest
        version = RadarArtifactVersion(
            artifact_version_id=f"artifact-version-{self._id_factory()}",
            artifact_id=artifact_id,
            task_id=task_id,
            artifact_type=artifact_type,
            version=len(versions) + 1,
            report=report,
            evidence_ledger=ledger,
            source_refs=source_refs,
            metadata=resolved_metadata,
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

    def export_doctor_material(
        self,
        task_id: str,
        *,
        confirmation_id: str,
    ) -> RadarArtifactVersion:
        """Create a versioned structured doctor export from the latest report."""

        task = self.get_task(task_id)
        confirmation = self.store.get_confirmation(confirmation_id)
        if (
            confirmation.task_id != task_id
            or confirmation.action_type
            not in {"export_doctor_material", "export_doctor_report"}
            or confirmation.status != "approved"
            or not confirmation.resolved_by
            or confirmation.requested_role not in {"elder", "family"}
        ):
            raise PermissionError(
                "doctor material export requires an approved user/family confirmation"
            )
        versions = self.store.list_task_artifact_versions(task_id)
        if confirmation.execution_status == "completed":
            existing_export = next(
                (
                    version
                    for version in versions
                    if version.artifact_version_id == confirmation.execution_ref
                    and version.artifact_type == "doctor_material_export"
                ),
                None,
            )
            if existing_export is None:
                raise ValueError("confirmed doctor export result is missing")
            return existing_export
        doctor_versions = [
            version
            for version in versions
            if version.report is not None and version.report.role == "doctor"
        ]
        if not doctor_versions:
            raise KeyError("no doctor report artifact is available for export")
        source_version = max(doctor_versions, key=lambda item: item.version)
        report = source_version.report
        assert report is not None
        payload = {
            "schema_version": "radar-doctor-material.v1",
            "task_id": task_id,
            "source_report_artifact_id": report.artifact_id,
            "source_report_version": source_version.version,
            "source_ledger_id": report.source_ledger_id,
            "risk_level": report.risk_level.value,
            "evidence_chain": report.structured_summary.get(
                "evidence_chain",
                [claim.model_dump(mode="json") for claim in report.facts],
            ),
            "data_quality": report.data_quality,
            "questionnaire_entries": [
                entry.model_dump(mode="json")
                for entry in report.questionnaire_entries
            ],
            "source_refs": report.source_refs,
            "caveats": report.caveats,
            "safety_notices": report.safety_notices,
            "non_diagnostic_boundary": True,
            "content": report.content,
            "generation_mode": report.generation_mode,
            "execution_mode": report.structured_summary.get("execution_mode"),
            "fact_snapshot_id": report.structured_summary.get(
                "fact_snapshot_id"
            ),
            "fact_snapshot_sha256": report.structured_summary.get(
                "fact_snapshot_sha256"
            ),
        }
        exported = self.save_payload_artifact(
            task_id,
            artifact_id=f"doctor-material:{task_id}",
            artifact_type="doctor_material_export",
            payload=payload,
            source_refs=[*report.claim_ids, *report.source_refs],
            metadata={
                "confirmation_id": confirmation_id,
                "source_artifact_version_id": source_version.artifact_version_id,
                "generation_mode": report.generation_mode,
                "execution_mode": report.structured_summary.get(
                    "execution_mode"
                ),
                "fact_snapshot_id": report.structured_summary.get(
                    "fact_snapshot_id"
                ),
                "fact_snapshot_sha256": report.structured_summary.get(
                    "fact_snapshot_sha256"
                ),
            },
        )
        self._record_event(
            task,
            "doctor_material.exported",
            "Structured doctor material was exported.",
            {
                "artifact_id": exported.artifact_id,
                "version": exported.version,
                "confirmation_id": confirmation_id,
            },
        )
        self._audit(
            task,
            "doctor_material_exported",
            exported.artifact_id,
            "Confirmed structured doctor material export created.",
        )
        self.complete_confirmation_action(
            task_id,
            confirmation_id,
            actor_id="task-service",
            actor_role="system",
            execution_ref=exported.artifact_version_id,
        )
        return exported

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
        task = self.get_task(task_id)
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

    def request_confirmation(
        self,
        task_id: str,
        request: HumanConfirmationRequest,
    ) -> HumanConfirmationRequest:
        with self._lock:
            task = self.get_task(task_id)
            if request.task_id != task_id or request.status != "pending":
                raise ValueError("confirmation must be pending and belong to the task")
            canonical = canonical_action(request.action_type)
            rule = confirmation_rule(canonical)
            if rule is not None:
                if request.requested_role not in rule.allowed_roles:
                    raise PermissionError(
                        "requested role is not permitted by the confirmation matrix"
                    )
                request = request.model_copy(
                    update={
                        "action_type": canonical,
                        "allowed_roles": list(rule.allowed_roles),
                        "confirmation_kind": rule.confirmation_kind,
                        "blocks_daily_flow": rule.blocks_daily_flow,
                        "idempotency_key": request.idempotency_key
                        or f"{task_id}:{canonical}",
                        "delivery_status": (
                            "pending"
                            if rule.confirmation_kind == "delivery_record"
                            else request.delivery_status
                        ),
                    }
                )
            if request.created_at > self._clock():
                request = request.model_copy(update={"created_at": self._clock()})
            existing = _matching_confirmation(self.store.list_confirmations(task_id), request)
            if existing is not None:
                return existing
            self.store.save_confirmation(request)
            if request.blocks_daily_flow and task.status == RadarTaskStatus.RUNNING:
                task = self.transition_task(
                    task_id,
                    RadarTaskStatus.WAITING_FOR_CONFIRMATION,
                    message="Task is waiting for human confirmation.",
                )
            self._record_event(
                task,
                "confirmation.requested",
                request.reason,
                {"confirmation_id": request.confirmation_id, "action_type": request.action_type},
            )
            self._audit(task, "confirmation_requested", request.confirmation_id, request.reason)
            return request

    def resolve_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        approved: bool,
        actor_id: str,
        actor_role: str,
    ) -> HumanConfirmationRequest:
        with self._lock:
            task = self.get_task(task_id)
            request = self.store.get_confirmation(confirmation_id)
            target_status = "approved" if approved else "rejected"
            if request.task_id != task_id:
                raise ValueError("confirmation belongs to another task")
            if request.status != "pending":
                if (
                    request.status == target_status
                    and request.resolved_by == actor_id
                ):
                    return request
                raise ValueError("confirmation has already reached a terminal state")
            if actor_role not in request.allowed_roles:
                raise PermissionError("actor role cannot resolve this confirmation")
            resolved = request.model_copy(
                update={
                    "status": target_status,
                    "resolved_at": self._clock(),
                    "resolved_by": actor_id,
                }
            )
            self.store.save_confirmation(resolved)
            task = self._record_event(
                task,
                f"confirmation.{resolved.status}",
                f"Confirmation {resolved.status} by {actor_id}.",
                {"confirmation_id": confirmation_id, "actor_role": actor_role},
            )
            task = self._record_event(
                task,
                "confirmation.resolved",
                f"Confirmation resolved as {resolved.status}.",
                {
                    "confirmation_id": confirmation_id,
                    "actor_role": actor_role,
                    "resolution_status": resolved.status,
                },
            )
            self._audit(task, f"confirmation_{resolved.status}", confirmation_id, f"Resolved by {actor_id}.")
            pending = _blocking_pending(self.store.list_confirmations(task_id))
            if not pending and task.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION:
                self.transition_task(task_id, RadarTaskStatus.RUNNING, message="All confirmations resolved.")
            return resolved

    def expire_confirmations(self, task_id: str, *, before: datetime) -> list[HumanConfirmationRequest]:
        task = self.get_task(task_id)
        expired: list[HumanConfirmationRequest] = []
        for request in self.store.list_confirmations(task_id):
            if request.status == "pending" and request.created_at <= before:
                resolved = request.model_copy(
                    update={"status": "expired", "resolved_at": self._clock(), "resolved_by": "system"}
                )
                self.store.save_confirmation(resolved)
                task = self._record_event(
                    task,
                    "confirmation.expired",
                    "Confirmation request expired.",
                    {"confirmation_id": request.confirmation_id},
                )
                self._audit(task, "confirmation_expired", request.confirmation_id, "Confirmation expired.")
                expired.append(resolved)
        pending = _blocking_pending(self.store.list_confirmations(task_id))
        current = self.get_task(task_id)
        if expired and not pending and current.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION:
            self.transition_task(task_id, RadarTaskStatus.RUNNING, message="All confirmations resolved or expired.")
        return expired

    def revoke_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> HumanConfirmationRequest:
        with self._lock:
            task = self.get_task(task_id)
            request = self.store.get_confirmation(confirmation_id)
            if request.task_id != task_id:
                raise ValueError("confirmation belongs to another task")
            if request.status == "revoked":
                if request.revoked_by == actor_id and request.revocation_reason == reason:
                    return request
                raise ValueError("confirmation was already revoked differently")
            if request.status not in {"pending", "approved"}:
                raise ValueError("only pending or approved confirmations can be revoked")
            if request.execution_status == "completed":
                raise ValueError("executed confirmation cannot be revoked")
            if actor_role not in request.allowed_roles:
                raise PermissionError("actor role cannot revoke this confirmation")
            revoked = request.model_copy(
                update={
                    "status": "revoked",
                    "revoked_at": self._clock(),
                    "revoked_by": actor_id,
                    "revocation_reason": reason,
                }
            )
            self.store.save_confirmation(revoked)
            task = self._record_event(
                task,
                "confirmation.revoked",
                f"Confirmation revoked by {actor_id}.",
                {"confirmation_id": confirmation_id, "actor_role": actor_role},
            )
            self._audit(
                task,
                "confirmation_revoked",
                confirmation_id,
                f"Revoked by {actor_id}: {reason}",
            )
            if (
                not _blocking_pending(self.store.list_confirmations(task_id))
                and task.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION
            ):
                self.transition_task(
                    task_id,
                    RadarTaskStatus.RUNNING,
                    message="All blocking confirmations resolved or revoked.",
                )
            return revoked

    def complete_confirmation_action(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        execution_ref: str,
        delivery_status: str | None = None,
    ) -> HumanConfirmationRequest:
        """Record an approved action exactly once after its actual executor succeeds."""

        with self._lock:
            task = self.get_task(task_id)
            request = self.store.get_confirmation(confirmation_id)
            if request.task_id != task_id or request.status != "approved":
                raise PermissionError("action requires an approved confirmation")
            if actor_role != "system" and actor_role not in request.allowed_roles:
                raise PermissionError("actor role cannot complete this action")
            if request.execution_status == "completed":
                if (
                    request.execution_ref == execution_ref
                    and (
                        delivery_status is None
                        or request.delivery_status == delivery_status
                    )
                ):
                    return request
                raise ValueError("confirmation action already completed with another result")
            resolved_delivery = delivery_status or request.delivery_status
            if resolved_delivery not in {
                "not_applicable",
                "pending",
                "delivered",
                "failed",
            }:
                raise ValueError("unsupported delivery status")
            now = self._clock()
            completed = request.model_copy(
                update={
                    "execution_status": "completed",
                    "execution_ref": execution_ref,
                    "executed_at": now,
                    "delivery_status": resolved_delivery,
                    "delivered_at": (
                        now if resolved_delivery == "delivered" else request.delivered_at
                    ),
                }
            )
            self.store.save_confirmation(completed)
            task = self._record_event(
                task,
                "confirmation.action_completed",
                f"Confirmed action completed by {actor_id}.",
                {
                    "confirmation_id": confirmation_id,
                    "action_type": request.action_type,
                    "execution_ref": execution_ref,
                    "delivery_status": resolved_delivery,
                },
            )
            self._audit(
                task,
                "confirmation_action_completed",
                confirmation_id,
                f"Execution recorded as {execution_ref}.",
            )
            return completed

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

    def persist_memory_decision(
        self,
        task_id: str,
        decision: MemoryWriteDecision,
    ):
        """Persist only a privacy-reviewed write approved by the Orchestrator."""

        with self._lock:
            task = self.get_task(task_id)
            if decision.task_id != task_id:
                raise ValueError("memory decision belongs to another task")
            if not decision.approved_for_write or decision.memory is None:
                raise PermissionError("memory write was not approved by the Orchestrator")
            if decision.candidate is None:
                raise PermissionError("memory decision is missing its reviewed candidate")
            memory = decision.memory
            if (
                memory.task_id != task_id
                or not memory.privacy_reviewed
                or memory.source_candidate_id != decision.candidate_id
                or memory.confirmation_id != decision.confirmation_id
            ):
                raise PermissionError("memory decision failed persistence integrity checks")
            candidate = decision.candidate
            privacy = MemoryPrivacyFilter().review(candidate)
            if (
                candidate.candidate_id != decision.candidate_id
                or candidate.task_id != task_id
                or not privacy.allowed
            ):
                raise PermissionError("memory candidate failed authoritative privacy review")
            if (
                memory.subject_id != candidate.subject_id
                or memory.memory_type != candidate.memory_type
                or memory.summary != candidate.summary
                or memory.payload != privacy.sanitized_payload
                or memory.evidence_refs != candidate.evidence_refs
            ):
                raise PermissionError("persisted memory does not match the reviewed candidate")
            try:
                confirmation = self.store.get_confirmation(decision.confirmation_id or "")
            except KeyError as exc:
                raise PermissionError("persisted family confirmation was not found") from exc
            if (
                confirmation.task_id != task_id
                or confirmation.action_type != candidate.confirmation_action
                or confirmation.requested_role != "family"
                or confirmation.status != "approved"
                or not confirmation.resolved_by
            ):
                raise PermissionError("persisted family confirmation is not valid for this write")
            existing = {
                item.memory_summary_id: item
                for item in self.store.list_memory_summaries(memory.subject_id)
            }.get(memory.memory_summary_id)
            if existing is not None:
                return existing
            saved = self.store.save_memory_summary(memory)
            self._record_event(
                task,
                "memory.written",
                "Approved long-term memory summary was written.",
                {
                    "memory_summary_id": memory.memory_summary_id,
                    "memory_type": memory.memory_type,
                    "confirmation_id": memory.confirmation_id,
                },
            )
            self._audit(
                task,
                "memory_written",
                memory.memory_summary_id,
                "Orchestrator-approved privacy-reviewed memory was persisted.",
            )
            return saved

    def execute(
        self,
        task_id: str,
        orchestrator: OrchestratorAgent,
        context: ContextPacket,
    ) -> OrchestratorDecision:
        task = self.get_task(task_id)
        if context.task_context.task_id != task_id:
            raise ValueError("context task_id does not match lifecycle task")
        if self._require_authorization:
            self._require_task_processing_authorization(task, context)
        if task.status == RadarTaskStatus.CREATED:
            task = self.transition_task(task_id, RadarTaskStatus.RUNNING)
        elif task.status != RadarTaskStatus.RUNNING:
            raise InvalidTaskTransition("task must be created or running before execution")
        self._audit(task, "orchestrator_started", task_id, "Single-task orchestration started.")
        try:
            decision = orchestrator.run(context)
            if decision.task_id != task_id:
                raise ValueError("orchestrator decision task_id does not match lifecycle task")
            self._sync_orchestrator_nodes(task_id, orchestrator)
            for result in decision.accepted_results:
                self.record_audit(
                    task_id,
                    actor=result.agent_name.value,
                    action="agent_completed",
                    target_ref=result.agent_name.value,
                    summary=f"{result.agent_name.value} completed with structured output.",
                    payload={
                        "skill_version": result.skill_version,
                        "prompt_version": result.prompt_version,
                        "evidence_ref_count": len(result.evidence_refs),
                        "claim_count": len(result.claims),
                        "safety_flags": result.safety_flags,
                    },
                )
                self.emit_event(
                    task_id,
                    event_type="agent.completed",
                    message=f"{result.agent_name.value} completed.",
                    payload={
                        "agent_name": result.agent_name.value,
                        "skill_version": result.skill_version,
                        "prompt_version": result.prompt_version,
                        "evidence_refs": result.evidence_refs,
                        "claim_count": len(result.claims),
                        "safety_flags": result.safety_flags,
                    },
                )
            for message in decision.a2a_messages:
                if message.task_id != task_id:
                    raise ValueError("A2A message task_id does not match lifecycle task")
                self.store.save_a2a_message(message)
                self.emit_event(
                    task_id,
                    event_type="a2a.message",
                    message=f"{message.sender} requested {message.intent} from {message.receiver}.",
                    payload={
                        "message_id": message.message_id,
                        "sender": message.sender,
                        "receiver": message.receiver,
                        "intent": message.intent,
                        "evidence_refs": message.evidence_refs,
                        "risk_level": message.risk_level.value,
                        "message_status": message.message_status,
                    },
                )
                self._audit(
                    self.get_task(task_id),
                    "a2a_message_forwarded",
                    message.message_id,
                    f"Forwarded {message.intent} from {message.sender} to {message.receiver}.",
                )
            for conflict in decision.conflicts:
                if conflict.task_id != task_id:
                    raise ValueError("conflict task_id does not match lifecycle task")
                self.store.save_conflict(conflict)
                self.emit_event(
                    task_id,
                    event_type="conflict.resolved",
                    message=conflict.summary,
                    payload={
                        "conflict_id": conflict.conflict_id,
                        "sources": conflict.sources,
                        "final_status": conflict.final_status,
                        "evidence_refs": conflict.evidence_refs,
                        "requires_human_confirmation": conflict.requires_human_confirmation,
                    },
                )
                self._audit(
                    self.get_task(task_id),
                    "conflict_resolved",
                    conflict.conflict_id,
                    f"Conflict resolved as {conflict.final_status}.",
                )
            if decision.evidence_ledger is not None:
                self.save_artifact(task_id, decision.evidence_ledger)
                for claim in decision.evidence_ledger.claims:
                    self.emit_event(
                        task_id,
                        event_type="claim.created",
                        message=claim.text,
                        payload={
                            "claim_id": claim.claim_id,
                            "generated_by": claim.generated_by,
                            "confidence": claim.confidence,
                            "risk_level": claim.risk_level.value,
                            "evidence_refs": claim.evidence_refs,
                        },
                    )
            for candidate in decision.questionnaire_candidates:
                self.emit_event(
                    task_id,
                    event_type="questionnaire.candidate",
                    message=f"Selected reviewed questionnaire item {candidate.question_id}.",
                    payload={
                        "question_id": candidate.question_id,
                        "question_source": candidate.question_source,
                        "source_id": candidate.source_id,
                        "source_version": candidate.source_version,
                        "policy_id": candidate.policy_id,
                        "policy_version": candidate.policy_version,
                    },
                )
            for result in decision.accepted_results:
                if result.agent_name != RadarAgentName.REPORT:
                    continue
                invocation_by_role = {
                    str(item.get("role")): item
                    for item in result.output_payload.get("llm_invocations", [])
                    if isinstance(item, dict) and item.get("role")
                }
                for payload in result.output_payload.get("reports", []):
                    report = RoleReportArtifact.model_validate(payload)
                    self.save_artifact(
                        task_id,
                        report,
                        metadata={
                            "agent_prompt_version": result.prompt_version,
                            "llm_invocation": invocation_by_role.get(report.role),
                        },
                    )
                    invocation = invocation_by_role.get(report.role) or {}
                    self.emit_event(
                        task_id,
                        event_type={
                            "fallback": "llm.fallback",
                            "llm": "llm.generation",
                            "template": "llm.skipped",
                        }[report.generation_mode],
                        message=f"Generated {report.role} report using {report.generation_mode} mode.",
                        payload={
                            "role": report.role,
                            "generation_mode": report.generation_mode,
                            "model_provider": report.model_provider,
                            "model_id": report.model_id,
                            "prompt_version": report.prompt_version,
                            "output_schema_status": invocation.get("output_schema_status"),
                            "fallback_reason": invocation.get("fallback_reason"),
                            "artifact_id": report.artifact_id,
                        },
                    )
                    self._audit(
                        self.get_task(task_id),
                        {
                            "fallback": "llm_fallback",
                            "llm": "llm_invocation",
                            "template": "llm_skipped",
                        }[report.generation_mode],
                        report.artifact_id,
                        (
                            f"{report.role} report generation mode: "
                            f"{report.generation_mode}."
                        ),
                    )
            automatic = list(
                dict.fromkeys(
                    action
                    for result in decision.accepted_results
                    for action in result.output_payload.get("alert_care", {}).get(
                        "automatic_actions", []
                    )
                )
            )
            for action in automatic:
                task = self._record_event(
                    self.get_task(task_id),
                    "action.auto_published",
                    f"Automatically published {action}.",
                    {"action_type": action},
                )
                self._audit(
                    task,
                    "automatic_action_published",
                    action,
                    "HumanConfirmation Matrix allows automatic publication.",
                )
            for request in decision.confirmation_requests:
                self.request_confirmation(task_id, request)
            self.save_payload_artifact(
                task_id,
                artifact_id=f"workflow:{task_id}",
                artifact_type="workflow_run",
                payload=_workflow_run_payload(
                    decision,
                    artifacts=self.list_artifacts(task_id),
                ),
                source_refs=(
                    decision.evidence_ledger.canonical_evidence_refs
                    if decision.evidence_ledger is not None
                    else []
                ),
                metadata={
                    "schema_version": "radar-workflow-run.v1",
                    "trace_id": self.get_task(task_id).trace_id,
                    "scenario": self.get_task(task_id).scenario,
                },
            )
            self._audit(
                self.get_task(task_id),
                "workflow_published",
                f"workflow:{task_id}",
                "Published minimized workflow run snapshot and versioned artifacts.",
            )
            if (
                not _blocking_pending(self.store.list_confirmations(task_id))
                and self.get_task(task_id).status == RadarTaskStatus.RUNNING
            ):
                self.transition_task(task_id, RadarTaskStatus.COMPLETED)
            self._audit(task, "orchestrator_completed", task_id, decision.decision_summary)
            return decision
        except Exception as exc:
            current = self.get_task(task_id)
            if current.status in {
                RadarTaskStatus.RUNNING,
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
            }:
                self._sync_orchestrator_nodes(task_id, orchestrator, include_failed=True)
                failed_node = getattr(orchestrator, "last_failed_node", None)
                failed_node_name = (
                    failed_node.value if hasattr(failed_node, "value") else failed_node
                )
                self.fail_task(
                    task_id,
                    error_code="orchestrator_error",
                    message=str(exc) or exc.__class__.__name__,
                    failed_node=failed_node_name,
                    details={
                        "exception_type": exc.__class__.__name__,
                        "failed_node": failed_node_name,
                    },
                )
            raise

    def _sync_orchestrator_nodes(
        self,
        task_id: str,
        orchestrator: OrchestratorAgent,
        *,
        include_failed: bool = False,
    ) -> None:
        completed = list(getattr(orchestrator, "last_completed_nodes", []))
        for node in completed:
            node_name = node.value if hasattr(node, "value") else str(node)
            status = self.get_task(task_id).node_status.get(
                node_name, RadarNodeStatus.PENDING
            )
            if status == RadarNodeStatus.PENDING:
                self.transition_node(task_id, node_name, RadarNodeStatus.RUNNING)
                status = RadarNodeStatus.RUNNING
            if status == RadarNodeStatus.RUNNING:
                self.transition_node(task_id, node_name, RadarNodeStatus.SUCCEEDED)
        if not include_failed:
            return
        failed = getattr(orchestrator, "last_failed_node", None)
        if failed is None:
            return
        failed_name = failed.value if hasattr(failed, "value") else str(failed)
        status = self.get_task(task_id).node_status.get(
            failed_name, RadarNodeStatus.PENDING
        )
        if status == RadarNodeStatus.PENDING:
            self.transition_node(task_id, failed_name, RadarNodeStatus.RUNNING)

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

    def _require_task_processing_authorization(
        self,
        task: RadarAgentTask,
        context: ContextPacket,
    ) -> None:
        if not task.authorization_id or not task.requested_by_user_id:
            raise AuthorizationRequired("task has no processing authorization")
        required_scopes = {"process_radar_summary"}
        ledger = context.evidence_packet.evidence_ledger
        if context.evidence_packet.questionnaire_entries or (
            ledger is not None and ledger.questionnaire_entries
        ):
            required_scopes.add("process_questionnaire")
        if context.evidence_packet.supplementary_documents or (
            ledger is not None and ledger.supplementary_documents
        ):
            required_scopes.add("process_supplementary_document")
        self._require_data_authorization(
            subject_id=task.subject_id,
            authorization_id=task.authorization_id,
            required_scopes=required_scopes,
        )
        self._require_actor_permission(
            subject_id=task.subject_id,
            actor_id=task.requested_by_user_id,
            role_binding_ids=task.role_binding_ids,
            allowed_roles={"elder", "family", "doctor"},
            permission="process_health_data",
        )

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
        persisted_sequence = events[-1].sequence if events else 0
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


def _workflow_run_payload(
    decision: OrchestratorDecision,
    *,
    artifacts: list[RadarArtifactVersion],
) -> dict[str, Any]:
    by_agent = {result.agent_name: result for result in decision.accepted_results}
    rag = by_agent.get(RadarAgentName.RAG)
    risk = by_agent.get(RadarAgentName.RISK_SIGNAL)
    alert = by_agent.get(RadarAgentName.ALERT_CARE)
    memory = by_agent.get(RadarAgentName.MEMORY)
    memory_candidates = (
        memory.output_payload.get("memory_proposal", {}).get("candidates", [])
        if memory is not None
        else []
    )
    return {
        "schema_version": "radar-workflow-run.v1",
        "task_id": decision.task_id,
        "terminal_node": decision.node.value,
        "decision_summary": decision.decision_summary,
        "agents": [
            {
                "agent_name": result.agent_name.value,
                "skill_version": result.skill_version,
                "prompt_version": result.prompt_version,
                "evidence_refs": result.evidence_refs,
                "claim_ids": [claim.claim_id for claim in result.claims],
                "safety_flags": result.safety_flags,
            }
            for result in decision.accepted_results
        ],
        "questionnaire_candidates": [
            item.model_dump(mode="json")
            for item in decision.questionnaire_candidates
        ],
        "a2a": [
            {
                "message_id": item.message_id,
                "sender": item.sender,
                "receiver": item.receiver,
                "intent": item.intent,
                "message_status": item.message_status,
                "evidence_refs": item.evidence_refs,
            }
            for item in decision.a2a_messages
        ],
        "conflicts": [
            {
                "conflict_id": item.conflict_id,
                "final_status": item.final_status,
                "evidence_refs": item.evidence_refs,
            }
            for item in decision.conflicts
        ],
        "rag": {
            "evidence_refs": rag.evidence_refs if rag is not None else [],
            "reviewed_seed_only": (
                "reviewed_seed_only" in rag.safety_flags
                if rag is not None
                else False
            ),
        },
        "risk_decision": (
            risk.output_payload.get("risk_signal", {})
            if risk is not None
            else {}
        ),
        "alert_decision": (
            alert.output_payload.get("alert_care", {})
            if alert is not None
            else {}
        ),
        "memory_candidates": [
            {
                "candidate_id": item.get("candidate_id"),
                "memory_type": item.get("memory_type"),
                "summary": item.get("summary"),
                "evidence_refs": item.get("evidence_refs", []),
                "privacy_tags": item.get("privacy_tags", []),
                "requires_confirmation": item.get("requires_confirmation", True),
            }
            for item in memory_candidates
            if isinstance(item, dict)
        ],
        "memory_rejected_inputs": (
            memory.output_payload.get("memory_proposal", {}).get(
                "rejected_inputs", []
            )
            if memory is not None
            else []
        ),
        "confirmations": [
            {
                "confirmation_id": item.confirmation_id,
                "action_type": item.action_type,
                "requested_role": item.requested_role,
                "confirmation_kind": item.confirmation_kind,
                "blocks_daily_flow": item.blocks_daily_flow,
            }
            for item in decision.confirmation_requests
        ],
        "published_artifacts": [
            {
                "artifact_version_id": item.artifact_version_id,
                "artifact_id": item.artifact_id,
                "artifact_type": item.artifact_type,
                "version": item.version,
            }
            for item in artifacts
        ],
    }


def _matching_confirmation(
    existing: list[HumanConfirmationRequest],
    requested: HumanConfirmationRequest,
) -> HumanConfirmationRequest | None:
    matched = next(
        (
            item
            for item in existing
            if item.confirmation_id == requested.confirmation_id
            or (
                requested.idempotency_key
                and item.idempotency_key == requested.idempotency_key
            )
        ),
        None,
    )
    if matched is None:
        return None
    if (
        matched.task_id != requested.task_id
        or matched.action_type != requested.action_type
        or matched.requested_role != requested.requested_role
        or matched.allowed_roles != requested.allowed_roles
        or matched.evidence_refs != requested.evidence_refs
    ):
        raise IdempotencyConflict(
            "confirmation idempotency key is bound to another confirmation intent"
        )
    return matched


def _blocking_pending(
    confirmations: list[HumanConfirmationRequest],
) -> list[HumanConfirmationRequest]:
    return [
        item
        for item in confirmations
        if item.status == "pending" and item.blocks_daily_flow
    ]


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
    "InvalidNodeTransition",
    "InvalidTaskTransition",
    "RoleAccessDenied",
    "TaskService",
]
