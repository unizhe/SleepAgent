from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from sleepagent.radar_agent.dynamic import (
    AgentInvocation,
    CompletionReceipt,
    DynamicStepRecord,
    ExecutionPlan,
    FactSnapshot,
    JobLease,
    ModelInvocation,
    RuntimeBudget,
    RuntimeCheckpoint,
    ToolInvocation,
    UserGoal,
    UserInputRequest,
    UserInputResponse,
)

from sleepagent.radar_agent.persistence.migrations import (
    RADAR_AGENT_POSTGRES_MIGRATION_SQL,
    RADAR_AGENT_POSTGRES_MIGRATIONS,
    MIGRATION_VERSIONS,
    apply_sqlite_migration,
    split_sql_statements,
)
from sleepagent.radar_agent.persistence.models import (
    RadarAlertRecord,
    RadarAuditLogEntry,
    RadarDataAuthorization,
    RadarMemorySummary,
    RadarReportArtifactVersion,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.runtime.contracts import (
    RadarAgentTask,
    RadarArtifactVersion,
    RadarTaskEvent,
    RadarTaskStatus,
)
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    ConflictRecord,
    EvidenceClaim,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
    RoleReportArtifact,
)


class RadarPersistenceStore:
    """DB-API persistence facade for the radar-agent PostgreSQL schema."""

    def __init__(self, connection: Any, *, dialect: str = "postgres") -> None:
        self.connection = connection
        self.dialect = dialect
        self._lock = RLock()
        if dialect == "sqlite":
            self.connection.execute("PRAGMA foreign_keys = ON")

    @property
    def transaction_lock(self) -> RLock:
        """Serialize transactions that share this DB-API connection."""

        return self._lock

    @classmethod
    def connect_sqlite(cls, connection: sqlite3.Connection) -> "RadarPersistenceStore":
        apply_sqlite_migration(connection)
        return cls(connection, dialect="sqlite")

    def save_subject(self, subject: RadarSubject) -> RadarSubject:
        self._execute(
            """
            INSERT INTO radar_subjects (
              subject_id, display_name, timezone_name, subject_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
              display_name = excluded.display_name,
              timezone_name = excluded.timezone_name,
              subject_json = excluded.subject_json,
              updated_at = excluded.updated_at
            """,
            (
                subject.subject_id,
                subject.display_name,
                subject.timezone_name,
                _dump_json(subject),
                _dump_datetime(subject.created_at),
                _dump_datetime(subject.updated_at),
            ),
        )
        return subject

    def get_subject(self, subject_id: str) -> RadarSubject:
        return self._get_json_model(
            "SELECT subject_json FROM radar_subjects WHERE subject_id = ?",
            (subject_id,),
            RadarSubject,
            f"radar subject not found: {subject_id}",
        )

    def save_role_binding(self, binding: RadarUserRoleBinding) -> RadarUserRoleBinding:
        self._execute(
            """
            INSERT INTO radar_user_roles (
              role_binding_id, user_id, subject_id, role, display_name,
              permissions_json, role_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(role_binding_id) DO UPDATE SET
              role = excluded.role,
              display_name = excluded.display_name,
              permissions_json = excluded.permissions_json,
              role_json = excluded.role_json,
              updated_at = excluded.updated_at
            """,
            (
                binding.role_binding_id,
                binding.user_id,
                binding.subject_id,
                binding.role,
                binding.display_name,
                _dump_plain_json(binding.permissions),
                _dump_json(binding),
                _dump_datetime(binding.created_at),
                _dump_datetime(binding.updated_at),
            ),
        )
        return binding

    def list_role_bindings(self, subject_id: str) -> list[RadarUserRoleBinding]:
        return self._list_json_models(
            """
            SELECT role_json FROM radar_user_roles
            WHERE subject_id = ?
            ORDER BY role, role_binding_id
            """,
            (subject_id,),
            RadarUserRoleBinding,
        )

    def save_data_authorization(
        self,
        authorization: RadarDataAuthorization,
    ) -> RadarDataAuthorization:
        self._execute(
            """
            INSERT INTO radar_data_authorizations (
              authorization_id, subject_id, granted_by_user_id, granted_by_role,
              status, scopes_json, authorization_json, granted_at, expires_at,
              revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(authorization_id) DO UPDATE SET
              status = excluded.status,
              scopes_json = excluded.scopes_json,
              authorization_json = excluded.authorization_json,
              expires_at = excluded.expires_at,
              revoked_at = excluded.revoked_at
            """,
            (
                authorization.authorization_id,
                authorization.subject_id,
                authorization.granted_by_user_id,
                authorization.granted_by_role,
                authorization.status,
                _dump_plain_json(authorization.scopes),
                _dump_json(authorization),
                _dump_datetime(authorization.granted_at),
                _dump_optional_datetime(authorization.expires_at),
                _dump_optional_datetime(authorization.revoked_at),
            ),
        )
        return authorization

    def get_data_authorization(
        self,
        authorization_id: str,
    ) -> RadarDataAuthorization:
        return self._get_json_model(
            """
            SELECT authorization_json FROM radar_data_authorizations
            WHERE authorization_id = ?
            """,
            (authorization_id,),
            RadarDataAuthorization,
            f"data authorization not found: {authorization_id}",
        )

    def list_data_authorizations(
        self,
        subject_id: str,
    ) -> list[RadarDataAuthorization]:
        return self._list_json_models(
            """
            SELECT authorization_json FROM radar_data_authorizations
            WHERE subject_id = ? ORDER BY granted_at, authorization_id
            """,
            (subject_id,),
            RadarDataAuthorization,
        )

    def save_task(self, task: RadarAgentTask) -> RadarAgentTask:
        existing = self._fetchone(
            "SELECT task_json FROM radar_tasks WHERE task_id = ?", (task.task_id,)
        )
        if existing is not None:
            persisted = RadarAgentTask.model_validate_json(existing[0])
            immutable_before = (
                persisted.runtime_kind,
                persisted.runtime_contract_version,
            )
            immutable_after = (task.runtime_kind, task.runtime_contract_version)
            if immutable_before != immutable_after:
                raise ValueError(
                    "runtime_kind and runtime_contract_version are immutable after task creation"
                )
        task = RadarAgentTask.model_validate(task.model_dump(mode="python"))
        self._execute(
            """
            INSERT INTO radar_tasks (
              task_id, trace_id, subject_id, radar_device_id, role, scenario,
              status, idempotency_key, runtime_kind, runtime_contract_version,
              execution_mode, completion_status, task_version, task_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              status = excluded.status,
              role = excluded.role,
              scenario = excluded.scenario,
              execution_mode = excluded.execution_mode,
              completion_status = excluded.completion_status,
              task_version = excluded.task_version,
              task_json = excluded.task_json,
              updated_at = excluded.updated_at
            """,
            (
                task.task_id,
                task.trace_id,
                task.subject_id,
                task.radar_device_id,
                task.role,
                task.scenario,
                task.status.value,
                task.idempotency_key,
                task.runtime_kind,
                task.runtime_contract_version,
                task.execution_mode,
                task.completion_status,
                task.task_version,
                _dump_json(task),
                _dump_datetime(task.created_at),
                _dump_datetime(task.updated_at),
            ),
        )
        return task

    def save_task_if_version(
        self, task: RadarAgentTask, *, expected_version: int
    ) -> RadarAgentTask:
        """Optimistic write used by the dynamic controller state machine."""

        task = RadarAgentTask.model_validate(task.model_dump(mode="python"))
        if task.task_version != expected_version + 1:
            raise ValueError("optimistic task update must increment task_version once")
        with self._lock:
            cursor = self.connection.execute(
                self._sql(
                    """
                    UPDATE radar_tasks SET
                      status = ?, role = ?, scenario = ?, execution_mode = ?,
                      completion_status = ?, task_version = ?, task_json = ?,
                      updated_at = ?
                    WHERE task_id = ? AND task_version = ?
                    """
                ),
                (
                    task.status.value,
                    task.role,
                    task.scenario,
                    task.execution_mode,
                    task.completion_status,
                    task.task_version,
                    _dump_json(task),
                    _dump_datetime(task.updated_at),
                    task.task_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise RuntimeError("optimistic task version conflict")
            self.connection.commit()
        return task

    def save_user_goal(self, goal: UserGoal) -> UserGoal:
        task = self.get_task(goal.task_id)
        if task.runtime_kind != "dynamic_goal" or task.node_status:
            raise ValueError("dynamic goals require a clean dynamic task")
        if goal.subject_id != task.subject_id:
            raise ValueError("dynamic goal subject does not match task")
        row = self._fetchone(
            "SELECT goal_json FROM radar_dynamic_goals WHERE goal_id = ?",
            (goal.goal_id,),
        )
        if row is not None:
            existing = UserGoal.model_validate_json(row[0])
            if existing != goal:
                raise ValueError("accepted dynamic goals are immutable")
            return existing
        self._execute(
            """
            INSERT INTO radar_dynamic_goals (
              goal_id, task_id, goal_type, goal_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                goal.goal_id,
                goal.task_id,
                goal.goal_type.value,
                _dump_json(goal),
                _dump_datetime(goal.created_at),
            ),
        )
        return goal

    def get_user_goal(self, goal_id: str) -> UserGoal:
        return self._get_json_model(
            "SELECT goal_json FROM radar_dynamic_goals WHERE goal_id = ?",
            (goal_id,),
            UserGoal,
            f"dynamic goal not found: {goal_id}",
        )

    def get_task_goal(self, task_id: str) -> UserGoal:
        return self._get_json_model(
            """
            SELECT goal_json FROM radar_dynamic_goals
            WHERE task_id = ? ORDER BY created_at LIMIT 1
            """,
            (task_id,),
            UserGoal,
            f"dynamic goal not found for task: {task_id}",
        )

    def save_execution_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        task = self.get_task(plan.task_id)
        if task.runtime_kind != "dynamic_goal" or task.node_status:
            raise ValueError("execution plans require a clean dynamic task")
        goal = self.get_task_goal(plan.task_id)
        if plan.goal_id != goal.goal_id:
            raise ValueError("execution plan does not match the accepted goal")
        existing = self._fetchone(
            "SELECT plan_json FROM radar_dynamic_plan_revisions WHERE plan_id = ?",
            (plan.plan_id,),
        )
        if existing is not None:
            persisted = ExecutionPlan.model_validate_json(existing[0])
            if persisted != plan:
                raise ValueError("persisted execution plans are immutable")
            return persisted
        self._execute(
            """
            INSERT INTO radar_dynamic_plan_revisions (
              plan_id, task_id, goal_id, revision, supersedes_plan_id,
              plan_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO NOTHING
            """,
            (
                plan.plan_id,
                plan.task_id,
                plan.goal_id,
                plan.revision,
                plan.supersedes_plan_id,
                _dump_json(plan),
                _dump_datetime(plan.created_at),
            ),
        )
        for step in plan.steps:
            self.save_dynamic_step(
                DynamicStepRecord(task_id=plan.task_id, plan_id=plan.plan_id, step=step)
            )
        return plan

    def get_execution_plan(self, plan_id: str) -> ExecutionPlan:
        return self._get_json_model(
            "SELECT plan_json FROM radar_dynamic_plan_revisions WHERE plan_id = ?",
            (plan_id,),
            ExecutionPlan,
            f"dynamic plan not found: {plan_id}",
        )

    def list_execution_plans(self, task_id: str) -> list[ExecutionPlan]:
        return self._list_json_models(
            """
            SELECT plan_json FROM radar_dynamic_plan_revisions
            WHERE task_id = ? ORDER BY revision
            """,
            (task_id,),
            ExecutionPlan,
        )

    def save_dynamic_step(self, record: DynamicStepRecord) -> DynamicStepRecord:
        plan = self.get_execution_plan(record.plan_id)
        if plan.task_id != record.task_id:
            raise ValueError("dynamic step task does not match its execution plan")
        declared = next(
            (item for item in plan.steps if item.step_id == record.step.step_id),
            None,
        )
        if declared is None or declared != record.step:
            raise ValueError("dynamic step is not declared by its immutable plan")
        self._execute(
            """
            INSERT INTO radar_dynamic_steps (
              task_id, plan_id, step_id, ordinal, status, step_json,
              output_json, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id, step_id) DO UPDATE SET
              status = excluded.status,
              step_json = excluded.step_json,
              output_json = excluded.output_json,
              started_at = excluded.started_at,
              finished_at = excluded.finished_at
            """,
            (
                record.task_id,
                record.plan_id,
                record.step.step_id,
                record.step.ordinal,
                record.status,
                _dump_json(record),
                _dump_plain_json(record.output),
                _dump_optional_datetime(record.started_at),
                _dump_optional_datetime(record.finished_at),
            ),
        )
        return record

    def list_dynamic_steps(self, plan_id: str) -> list[DynamicStepRecord]:
        return self._list_json_models(
            """
            SELECT step_json FROM radar_dynamic_steps
            WHERE plan_id = ? ORDER BY ordinal
            """,
            (plan_id,),
            DynamicStepRecord,
        )

    def save_model_invocation(self, value: ModelInvocation) -> ModelInvocation:
        self._execute(
            """
            INSERT INTO radar_model_invocations (
              invocation_id, task_id, purpose, agent, client_idempotency_key,
              attempt, outcome, provider_request_id, invocation_json,
              started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
              outcome = excluded.outcome,
              provider_request_id = excluded.provider_request_id,
              invocation_json = excluded.invocation_json,
              finished_at = excluded.finished_at
            """,
            (
                value.invocation_id,
                value.task_id,
                value.purpose,
                value.agent.value,
                value.client_idempotency_key,
                value.attempt,
                value.outcome.value,
                value.provider_request_id,
                _dump_json(value),
                _dump_datetime(value.started_at),
                _dump_optional_datetime(value.finished_at),
            ),
        )
        return value

    def list_model_invocations(self, task_id: str) -> list[ModelInvocation]:
        return self._list_json_models(
            """
            SELECT invocation_json FROM radar_model_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            ModelInvocation,
        )

    def save_agent_invocation(self, value: AgentInvocation) -> AgentInvocation:
        self._execute(
            """
            INSERT INTO radar_agent_invocations (
              invocation_id, task_id, plan_id, step_id, agent, status,
              caused_by_a2a_message_id, invocation_json, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
              status = excluded.status,
              invocation_json = excluded.invocation_json,
              finished_at = excluded.finished_at
            """,
            (
                value.invocation_id,
                value.task_id,
                value.plan_id,
                value.step_id,
                value.agent.value,
                value.status.value,
                value.caused_by_a2a_message_id,
                _dump_json(value),
                _dump_datetime(value.started_at),
                _dump_optional_datetime(value.finished_at),
            ),
        )
        return value

    def list_agent_invocations(self, task_id: str) -> list[AgentInvocation]:
        return self._list_json_models(
            """
            SELECT invocation_json FROM radar_agent_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            AgentInvocation,
        )

    def save_tool_invocation(self, value: ToolInvocation) -> ToolInvocation:
        self._execute(
            """
            INSERT INTO radar_tool_invocations (
              invocation_id, task_id, plan_id, step_id, tool_name,
              client_idempotency_key, attempt, outcome, invocation_json,
              started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
              outcome = excluded.outcome,
              invocation_json = excluded.invocation_json,
              finished_at = excluded.finished_at
            """,
            (
                value.invocation_id,
                value.task_id,
                value.plan_id,
                value.step_id,
                value.tool_name,
                value.client_idempotency_key,
                value.attempt,
                value.outcome.value,
                _dump_json(value),
                _dump_datetime(value.started_at),
                _dump_optional_datetime(value.finished_at),
            ),
        )
        return value

    def list_tool_invocations(self, task_id: str) -> list[ToolInvocation]:
        return self._list_json_models(
            """
            SELECT invocation_json FROM radar_tool_invocations
            WHERE task_id = ? ORDER BY started_at, invocation_id
            """,
            (task_id,),
            ToolInvocation,
        )

    def save_user_input_request(self, value: UserInputRequest) -> UserInputRequest:
        existing = self._fetchone(
            "SELECT response_json FROM radar_user_input_requests WHERE request_id = ?",
            (value.request_id,),
        )
        response_json = existing[0] if existing is not None else None
        self._execute(
            """
            INSERT INTO radar_user_input_requests (
              request_id, task_id, question_id, status, request_json,
              response_json, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
              status = excluded.status,
              request_json = excluded.request_json,
              resolved_at = excluded.resolved_at
            """,
            (
                value.request_id,
                value.task_id,
                value.question_id,
                value.status,
                _dump_json(value),
                response_json,
                _dump_datetime(value.created_at),
                _dump_optional_datetime(value.resolved_at),
            ),
        )
        return value

    def get_user_input_request(self, request_id: str) -> UserInputRequest:
        return self._get_json_model(
            "SELECT request_json FROM radar_user_input_requests WHERE request_id = ?",
            (request_id,),
            UserInputRequest,
            f"user input request not found: {request_id}",
        )

    def list_user_input_requests(self, task_id: str) -> list[UserInputRequest]:
        return self._list_json_models(
            """
            SELECT request_json FROM radar_user_input_requests
            WHERE task_id = ? ORDER BY created_at, request_id
            """,
            (task_id,),
            UserInputRequest,
        )

    def save_user_input_response(self, value: UserInputResponse) -> UserInputResponse:
        request = self.get_user_input_request(value.request_id)
        if request.task_id != value.task_id:
            raise ValueError("user input response belongs to another task")
        if request.status != "pending":
            row = self._fetchone(
                "SELECT response_json FROM radar_user_input_requests WHERE request_id = ?",
                (value.request_id,),
            )
            if row is not None and row[0] == _dump_json(value):
                return value
            raise ValueError("user input request is no longer pending")
        if request.target_role != value.answered_by_role:
            raise ValueError("answering role does not match the reviewed request")
        if request.expires_at is not None and value.created_at > request.expires_at:
            raise ValueError("user input request has expired")
        if request.answer_options and value.answer not in request.answer_options:
            raise ValueError("answer is not one of the reviewed options")
        resolved_at = value.created_at
        updated = request.model_copy(update={"status": "answered", "resolved_at": resolved_at})
        self._execute(
            """
            UPDATE radar_user_input_requests
            SET status = ?, request_json = ?, response_json = ?, resolved_at = ?
            WHERE request_id = ?
            """,
            (
                "answered",
                _dump_json(updated),
                _dump_json(value),
                _dump_datetime(resolved_at),
                value.request_id,
            ),
        )
        return value

    def decline_user_input_request(
        self,
        request_id: str,
        *,
        declined_at: datetime | None = None,
    ) -> UserInputRequest:
        request = self.get_user_input_request(request_id)
        if request.status == "declined":
            return request
        if request.status != "pending":
            raise ValueError("user input request is no longer pending")
        resolved_at = declined_at or datetime.now(timezone.utc)
        updated = request.model_copy(
            update={"status": "declined", "resolved_at": resolved_at}
        )
        self._execute(
            """
            UPDATE radar_user_input_requests
            SET status = ?, request_json = ?, resolved_at = ?
            WHERE request_id = ?
            """,
            (
                "declined",
                _dump_json(updated),
                _dump_datetime(resolved_at),
                request_id,
            ),
        )
        return updated

    def get_user_input_response(self, request_id: str) -> UserInputResponse | None:
        row = self._fetchone(
            "SELECT response_json FROM radar_user_input_requests WHERE request_id = ?",
            (request_id,),
        )
        if row is None or row[0] is None:
            return None
        return UserInputResponse.model_validate_json(row[0])

    def save_runtime_budget(self, value: RuntimeBudget) -> RuntimeBudget:
        self._execute(
            """
            INSERT INTO radar_runtime_budgets (task_id, budget_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              budget_json = excluded.budget_json,
              updated_at = excluded.updated_at
            """,
            (value.task_id, _dump_json(value), _dump_datetime(value.updated_at)),
        )
        return value

    def get_runtime_budget(self, task_id: str) -> RuntimeBudget:
        return self._get_json_model(
            "SELECT budget_json FROM radar_runtime_budgets WHERE task_id = ?",
            (task_id,),
            RuntimeBudget,
            f"runtime budget not found: {task_id}",
        )

    def save_runtime_checkpoint(self, value: RuntimeCheckpoint) -> RuntimeCheckpoint:
        self._execute(
            """
            INSERT INTO radar_runtime_checkpoints (
              checkpoint_id, task_id, sequence, state_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(checkpoint_id) DO UPDATE SET state_json = excluded.state_json
            """,
            (
                value.checkpoint_id,
                value.task_id,
                value.sequence,
                _dump_json(value),
                _dump_datetime(value.created_at),
            ),
        )
        return value

    def latest_runtime_checkpoint(self, task_id: str) -> RuntimeCheckpoint | None:
        row = self._fetchone(
            """
            SELECT state_json FROM radar_runtime_checkpoints
            WHERE task_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (task_id,),
        )
        return RuntimeCheckpoint.model_validate_json(row[0]) if row else None

    def acquire_job_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> JobLease | None:
        current = now or datetime.now(timezone.utc)
        lease = JobLease(
            task_id=task_id,
            worker_id=worker_id,
            lease_token=uuid4().hex,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
            heartbeat_at=current,
        )
        self._execute(
            """
            INSERT INTO radar_job_leases (
              task_id, worker_id, lease_token, lease_expires_at, heartbeat_at, attempt
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              worker_id = excluded.worker_id,
              lease_token = excluded.lease_token,
              lease_expires_at = excluded.lease_expires_at,
              heartbeat_at = excluded.heartbeat_at,
              attempt = radar_job_leases.attempt + 1
            WHERE radar_job_leases.lease_expires_at <= ?
               OR radar_job_leases.worker_id = excluded.worker_id
            """,
            (
                lease.task_id,
                lease.worker_id,
                lease.lease_token,
                _dump_datetime(lease.lease_expires_at),
                _dump_datetime(lease.heartbeat_at),
                lease.attempt,
                _dump_datetime(current),
            ),
        )
        row = self._fetchone(
            """
            SELECT worker_id, lease_token, lease_expires_at, heartbeat_at, attempt
            FROM radar_job_leases WHERE task_id = ?
            """,
            (task_id,),
        )
        if row is None or row[1] != lease.lease_token:
            return None
        return JobLease(
            task_id=task_id,
            worker_id=row[0],
            lease_token=row[1],
            lease_expires_at=datetime.fromisoformat(row[2]),
            heartbeat_at=datetime.fromisoformat(row[3]),
            attempt=row[4],
        )

    def release_job_lease(self, task_id: str, *, lease_token: str) -> bool:
        row = self._fetchone(
            "SELECT lease_token FROM radar_job_leases WHERE task_id = ?", (task_id,)
        )
        if row is None or row[0] != lease_token:
            return False
        self._execute("DELETE FROM radar_job_leases WHERE task_id = ?", (task_id,))
        return True

    def save_fact_snapshot(self, value: FactSnapshot) -> FactSnapshot:
        row = self._fetchone(
            "SELECT snapshot_json FROM radar_fact_snapshots WHERE snapshot_id = ?",
            (value.snapshot_id,),
        )
        if row is not None:
            existing = FactSnapshot.model_validate_json(row[0])
            if existing != value:
                raise ValueError("accepted fact snapshots are immutable")
            return existing
        self._execute(
            """
            INSERT INTO radar_fact_snapshots (
              snapshot_id, task_id, sha256, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                value.snapshot_id,
                value.task_id,
                value.sha256,
                _dump_json(value),
                _dump_datetime(value.created_at),
            ),
        )
        return value

    def get_fact_snapshot(self, snapshot_id: str) -> FactSnapshot:
        return self._get_json_model(
            "SELECT snapshot_json FROM radar_fact_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
            FactSnapshot,
            f"fact snapshot not found: {snapshot_id}",
        )

    def save_completion_receipt(self, value: CompletionReceipt) -> CompletionReceipt:
        self._execute(
            """
            INSERT INTO radar_completion_receipts (
              receipt_id, task_id, execution_mode, completion_status,
              receipt_json, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(receipt_id) DO UPDATE SET receipt_json = excluded.receipt_json
            """,
            (
                value.receipt_id,
                value.task_id,
                value.execution_mode.value,
                value.completion_status.value,
                _dump_json(value),
                _dump_datetime(value.completed_at),
            ),
        )
        return value

    def get_completion_receipt(self, task_id: str) -> CompletionReceipt:
        return self._get_json_model(
            """
            SELECT receipt_json FROM radar_completion_receipts
            WHERE task_id = ? ORDER BY completed_at DESC LIMIT 1
            """,
            (task_id,),
            CompletionReceipt,
            f"completion receipt not found: {task_id}",
        )

    def get_task(self, task_id: str) -> RadarAgentTask:
        return self._get_json_model(
            "SELECT task_json FROM radar_tasks WHERE task_id = ?",
            (task_id,),
            RadarAgentTask,
            f"radar task not found: {task_id}",
        )

    def get_task_by_idempotency_key(self, idempotency_key: str) -> RadarAgentTask | None:
        row = self._fetchone(
            "SELECT task_json FROM radar_tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        return RadarAgentTask.model_validate_json(row[0]) if row is not None else None

    def list_tasks(
        self,
        *,
        statuses: set[RadarTaskStatus] | None = None,
    ) -> list[RadarAgentTask]:
        tasks = self._list_json_models(
            "SELECT task_json FROM radar_tasks ORDER BY created_at, task_id",
            (),
            RadarAgentTask,
        )
        if statuses is None:
            return tasks
        return [task for task in tasks if task.status in statuses]

    def save_task_event(self, event: RadarTaskEvent) -> RadarTaskEvent:
        self._execute(
            """
            INSERT INTO radar_task_events (
              event_id, task_id, trace_id, sequence, event_type, event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET event_json = excluded.event_json
            """,
            (
                event.event_id,
                event.task_id,
                event.trace_id,
                event.sequence,
                event.event_type,
                _dump_json(event),
                _dump_datetime(event.created_at),
            ),
        )
        return event

    def list_task_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[RadarTaskEvent]:
        return self._list_json_models(
            """
            SELECT event_json FROM radar_task_events
            WHERE task_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (task_id, after_sequence),
            RadarTaskEvent,
        )

    def delete_task(self, task_id: str) -> None:
        self._execute("DELETE FROM radar_tasks WHERE task_id = ?", (task_id,))

    def delete_subject_data(self, subject_id: str) -> dict[str, int]:
        """Delete one subject's radar product data, including detached audit rows."""

        task_ids = [
            task.task_id for task in self.list_tasks() if task.subject_id == subject_id
        ]
        device_rows = self._fetchall(
            "SELECT radar_device_id FROM radar_devices WHERE subject_id = ?",
            (subject_id,),
        )
        device_ids = [str(row[0]) for row in device_rows]
        counts = {
            "tasks": len(task_ids),
            "devices": len(device_ids),
            "authorizations": len(self.list_data_authorizations(subject_id)),
            "role_bindings": len(self.list_role_bindings(subject_id)),
            "night_summaries": len(self.list_night_summaries(subject_id)),
            "memory_summaries": len(self.list_memory_summaries(subject_id)),
            "alerts": len(self.list_alerts(subject_id)),
        }
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            self._execute(
                f"DELETE FROM radar_audit_logs WHERE task_id IN ({placeholders})",
                tuple(task_ids),
            )
        for radar_device_id in device_ids:
            self._execute(
                "DELETE FROM radar_devices WHERE radar_device_id = ?",
                (radar_device_id,),
            )
        self._execute("DELETE FROM radar_subjects WHERE subject_id = ?", (subject_id,))
        return counts

    def save_device(self, device: RadarDevice) -> RadarDevice:
        self._execute(
            """
            INSERT INTO radar_devices (
              radar_device_id, subject_id, provider, status, device_json, registered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(radar_device_id) DO UPDATE SET
              subject_id = excluded.subject_id,
              provider = excluded.provider,
              status = excluded.status,
              device_json = excluded.device_json,
              updated_at = excluded.updated_at
            """,
            (
                device.radar_device_id,
                device.bound_subject_id,
                device.provider,
                device.status.value,
                _dump_json(device),
                _dump_datetime(device.registered_at),
                _dump_datetime(device.updated_at),
            ),
        )
        return device

    def get_device(self, radar_device_id: str) -> RadarDevice:
        return self._get_json_model(
            "SELECT device_json FROM radar_devices WHERE radar_device_id = ?",
            (radar_device_id,),
            RadarDevice,
            f"radar device not found: {radar_device_id}",
        )

    def save_vital_snapshot(self, snapshot: RadarVitalSnapshot) -> RadarVitalSnapshot:
        self._execute(
            """
            INSERT INTO radar_vital_snapshots (
              snapshot_id, radar_device_id, subject_id, measured_at, received_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
              snapshot_json = excluded.snapshot_json,
              received_at = excluded.received_at
            """,
            (
                snapshot.snapshot_id,
                snapshot.radar_device_id,
                snapshot.subject_id,
                _dump_datetime(snapshot.measured_at),
                _dump_datetime(snapshot.received_at),
                _dump_json(snapshot),
            ),
        )
        return snapshot

    def list_vital_snapshots(self, radar_device_id: str) -> list[RadarVitalSnapshot]:
        return self._list_json_models(
            """
            SELECT snapshot_json FROM radar_vital_snapshots
            WHERE radar_device_id = ?
            ORDER BY measured_at, snapshot_id
            """,
            (radar_device_id,),
            RadarVitalSnapshot,
        )

    def save_night_summary(self, summary: RadarNightSummary) -> RadarNightSummary:
        summary_id = _night_summary_id(summary.radar_device_id, summary.night_of)
        self._execute(
            """
            INSERT INTO radar_night_summaries (
              summary_id, radar_device_id, subject_id, night_of, data_coverage_ratio,
              summary_json, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(summary_id) DO UPDATE SET
              data_coverage_ratio = excluded.data_coverage_ratio,
              summary_json = excluded.summary_json,
              generated_at = excluded.generated_at
            """,
            (
                summary_id,
                summary.radar_device_id,
                summary.subject_id,
                summary.night_of.isoformat(),
                summary.data_coverage_ratio,
                _dump_json(summary),
                _dump_datetime(summary.generated_at),
            ),
        )
        return summary

    def get_night_summary(self, radar_device_id: str, night_of: date) -> RadarNightSummary:
        return self._get_json_model(
            "SELECT summary_json FROM radar_night_summaries WHERE summary_id = ?",
            (_night_summary_id(radar_device_id, night_of),),
            RadarNightSummary,
            f"radar night summary not found: {radar_device_id} {night_of.isoformat()}",
        )

    def list_night_summaries(self, subject_id: str) -> list[RadarNightSummary]:
        return self._list_json_models(
            """
            SELECT summary_json FROM radar_night_summaries
            WHERE subject_id = ? ORDER BY night_of, summary_id
            """,
            (subject_id,),
            RadarNightSummary,
        )

    def save_evidence_ledger(self, ledger: EvidenceLedger) -> EvidenceLedger:
        self._execute(
            """
            INSERT INTO radar_evidence_ledgers (
              ledger_id, task_id, review_status, confidence, ledger_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ledger_id) DO UPDATE SET
              review_status = excluded.review_status,
              confidence = excluded.confidence,
              ledger_json = excluded.ledger_json,
              updated_at = excluded.updated_at
            """,
            (
                ledger.ledger_id,
                ledger.task_id,
                ledger.review_status.value,
                ledger.confidence,
                _dump_json(ledger),
                _dump_datetime(ledger.updated_at),
            ),
        )
        for claim in ledger.claims:
            self._save_evidence_claim(ledger.ledger_id, claim)
        return ledger

    def save_task_artifact_version(
        self,
        version: RadarArtifactVersion,
    ) -> RadarArtifactVersion:
        self._execute(
            """
            INSERT INTO radar_task_artifact_versions (
              artifact_version_id, artifact_id, task_id, artifact_type,
              version_number, artifact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_version_id) DO UPDATE SET
              artifact_json = excluded.artifact_json
            """,
            (
                version.artifact_version_id,
                version.artifact_id,
                version.task_id,
                version.artifact_type,
                version.version,
                _dump_json(version),
                _dump_datetime(version.created_at),
            ),
        )
        return version

    def list_task_artifact_versions(
        self,
        task_id: str,
        *,
        artifact_id: str | None = None,
    ) -> list[RadarArtifactVersion]:
        if artifact_id is None:
            sql = """
                SELECT artifact_json FROM radar_task_artifact_versions
                WHERE task_id = ? ORDER BY artifact_type, artifact_id, version_number
            """
            params = (task_id,)
        else:
            sql = """
                SELECT artifact_json FROM radar_task_artifact_versions
                WHERE task_id = ? AND artifact_id = ? ORDER BY version_number
            """
            params = (task_id, artifact_id)
        return self._list_json_models(sql, params, RadarArtifactVersion)

    def list_artifact_versions_by_artifact_id(
        self, artifact_id: str
    ) -> list[RadarArtifactVersion]:
        return self._list_json_models(
            """
            SELECT artifact_json FROM radar_task_artifact_versions
            WHERE artifact_id = ? ORDER BY version_number
            """,
            (artifact_id,),
            RadarArtifactVersion,
        )

    def save_confirmation(
        self,
        request: HumanConfirmationRequest,
    ) -> HumanConfirmationRequest:
        self._execute(
            """
            INSERT INTO radar_human_confirmations (
              confirmation_id, task_id, action_type, requested_role, status,
              confirmation_json, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(confirmation_id) DO UPDATE SET
              status = excluded.status,
              confirmation_json = excluded.confirmation_json,
              resolved_at = excluded.resolved_at
            """,
            (
                request.confirmation_id,
                request.task_id,
                request.action_type,
                request.requested_role,
                request.status,
                _dump_json(request),
                _dump_datetime(request.created_at),
                _dump_optional_datetime(request.resolved_at),
            ),
        )
        return request

    def get_confirmation(self, confirmation_id: str) -> HumanConfirmationRequest:
        return self._get_json_model(
            "SELECT confirmation_json FROM radar_human_confirmations WHERE confirmation_id = ?",
            (confirmation_id,),
            HumanConfirmationRequest,
            f"confirmation not found: {confirmation_id}",
        )

    def list_confirmations(self, task_id: str) -> list[HumanConfirmationRequest]:
        return self._list_json_models(
            """
            SELECT confirmation_json FROM radar_human_confirmations
            WHERE task_id = ? ORDER BY created_at, confirmation_id
            """,
            (task_id,),
            HumanConfirmationRequest,
        )

    def save_product_human_decision(
        self,
        *,
        decision_id: str,
        task_id: str | None,
        episode_id: str,
        subject_id: str,
        status: str,
        target_hash: str,
        decision_json: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_human_decisions (
              decision_id, task_id, episode_id, subject_id, status, target_hash,
              decision_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
              status = excluded.status,
              target_hash = excluded.target_hash,
              decision_json = excluded.decision_json,
              updated_at = excluded.updated_at
            """,
            (
                decision_id,
                task_id,
                episode_id,
                subject_id,
                status,
                target_hash,
                decision_json,
                _dump_datetime(created_at),
                _dump_datetime(updated_at),
            ),
        )

    def get_product_human_decision_json(self, decision_id: str) -> str:
        row = self._fetchone(
            """
            SELECT decision_json FROM product_human_decisions
            WHERE decision_id = ?
            """,
            (decision_id,),
        )
        if row is None:
            raise KeyError(f"human decision not found: {decision_id}")
        return _database_json_text(row[0])

    def list_product_human_decision_json(
        self,
        *,
        task_id: str | None = None,
        subject_id: str | None = None,
        status: str | None = None,
    ) -> list[str]:
        clauses: list[str] = []
        params: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            "SELECT decision_json FROM product_human_decisions"
            f"{where} ORDER BY created_at, decision_id",
            tuple(params),
        )
        return [_database_json_text(row[0]) for row in rows]

    def append_product_human_decision_event(
        self,
        *,
        event_id: str,
        decision_id: str,
        event_type: str,
        actor_id: str | None,
        event_json: str,
        created_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_human_decision_events (
              event_id, decision_id, event_type, actor_id, event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event_id,
                decision_id,
                event_type,
                actor_id,
                event_json,
                _dump_datetime(created_at),
            ),
        )

    def list_product_human_decision_event_json(
        self, decision_id: str
    ) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT event_json FROM product_human_decision_events
                WHERE decision_id = ? ORDER BY created_at, event_id
                """,
                (decision_id,),
            )
        ]

    def save_pending_habit_change_set(
        self,
        *,
        change_set_id: str,
        subject_id: str,
        state: str,
        payload_json: str,
        snapshot_json: str,
        expires_at: datetime,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_pending_habit_change_sets (
              change_set_id, subject_id, state, payload_json, snapshot_json,
              expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(change_set_id) DO UPDATE SET
              state = excluded.state,
              payload_json = excluded.payload_json,
              snapshot_json = excluded.snapshot_json,
              updated_at = excluded.updated_at
            """,
            (
                change_set_id,
                subject_id,
                state,
                payload_json,
                snapshot_json,
                _dump_datetime(expires_at),
                _dump_datetime(created_at),
                _dump_datetime(updated_at),
            ),
        )

    def get_pending_habit_change_set_row(
        self, change_set_id: str
    ) -> tuple[str, str, str, str, str, str, str]:
        row = self._fetchone(
            """
            SELECT subject_id, state, payload_json, snapshot_json, expires_at,
                   created_at, updated_at
            FROM product_pending_habit_change_sets
            WHERE change_set_id = ?
            """,
            (change_set_id,),
        )
        if row is None:
            raise KeyError(f"pending habit change set not found: {change_set_id}")
        return (
            str(row[0]),
            str(row[1]),
            _database_json_text(row[2]),
            _database_json_text(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
        )

    def list_pending_habit_change_set_rows(
        self,
        *,
        subject_id: str,
        state: str | None = None,
    ) -> list[tuple[str, str, str, str, str, str, str, str]]:
        if state is None:
            sql = """
                SELECT change_set_id, subject_id, state, payload_json, snapshot_json,
                       expires_at, created_at, updated_at
                FROM product_pending_habit_change_sets
                WHERE subject_id = ? ORDER BY created_at, change_set_id
            """
            params = (subject_id,)
        else:
            sql = """
                SELECT change_set_id, subject_id, state, payload_json, snapshot_json,
                       expires_at, created_at, updated_at
                FROM product_pending_habit_change_sets
                WHERE subject_id = ? AND state = ?
                ORDER BY created_at, change_set_id
            """
            params = (subject_id, state)
        return [
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                _database_json_text(row[3]),
                _database_json_text(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
            )
            for row in self._fetchall(sql, params)
        ]

    def get_evidence_ledger(self, ledger_id: str) -> EvidenceLedger:
        return self._get_json_model(
            "SELECT ledger_json FROM radar_evidence_ledgers WHERE ledger_id = ?",
            (ledger_id,),
            EvidenceLedger,
            f"radar evidence ledger not found: {ledger_id}",
        )

    def _save_evidence_claim(self, ledger_id: str, claim: EvidenceClaim) -> None:
        self._execute(
            """
            INSERT INTO radar_evidence_claims (
              claim_id, ledger_id, task_id, generated_by, risk_level, confidence, claim_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
              claim_json = excluded.claim_json,
              confidence = excluded.confidence,
              risk_level = excluded.risk_level
            """,
            (
                claim.claim_id,
                ledger_id,
                claim.task_id,
                claim.generated_by,
                claim.risk_level.value,
                claim.confidence,
                _dump_json(claim),
            ),
        )

    def save_a2a_message(self, message: A2AMessage) -> A2AMessage:
        self._execute(
            """
            INSERT INTO radar_a2a_messages (
              message_id, task_id, target_task_id, sender, receiver, intent, risk_level,
              collaboration_round, routed_by, shared_artifact_type,
              message_status, message_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
              target_task_id = excluded.target_task_id,
              collaboration_round = excluded.collaboration_round,
              routed_by = excluded.routed_by,
              shared_artifact_type = excluded.shared_artifact_type,
              message_status = excluded.message_status,
              message_json = excluded.message_json
            """,
            (
                message.message_id,
                message.task_id,
                message.target_task_id,
                message.sender,
                message.receiver,
                message.intent,
                message.risk_level.value,
                message.collaboration_round,
                message.routed_by,
                message.shared_artifact_type,
                message.message_status,
                _dump_json(message),
                _dump_datetime(message.created_at),
            ),
        )
        return message

    def list_a2a_messages(self, task_id: str) -> list[A2AMessage]:
        return self._list_json_models(
            """
            SELECT message_json FROM radar_a2a_messages
            WHERE task_id = ?
            ORDER BY created_at, message_id
            """,
            (task_id,),
            A2AMessage,
        )

    def save_conflict(self, conflict: ConflictRecord) -> ConflictRecord:
        self._execute(
            """
            INSERT INTO radar_conflict_records (
              conflict_id, task_id, final_status, requires_human_confirmation,
              conflict_json, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conflict_id) DO UPDATE SET
              final_status = excluded.final_status,
              requires_human_confirmation = excluded.requires_human_confirmation,
              conflict_json = excluded.conflict_json
            """,
            (
                conflict.conflict_id,
                conflict.task_id,
                conflict.final_status,
                int(conflict.requires_human_confirmation),
                _dump_json(conflict),
                _dump_datetime(conflict.decided_at),
            ),
        )
        return conflict

    def list_conflicts(self, task_id: str) -> list[ConflictRecord]:
        return self._list_json_models(
            """
            SELECT conflict_json FROM radar_conflict_records
            WHERE task_id = ?
            ORDER BY decided_at, conflict_id
            """,
            (task_id,),
            ConflictRecord,
        )

    def save_alert(self, alert: RadarAlertRecord) -> RadarAlertRecord:
        self._execute(
            """
            INSERT INTO radar_alerts (
              alert_id, task_id, subject_id, risk_level, status, alert_json, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alert_id) DO UPDATE SET
              status = excluded.status,
              alert_json = excluded.alert_json,
              resolved_at = excluded.resolved_at
            """,
            (
                alert.alert_id,
                alert.task_id,
                alert.subject_id,
                alert.risk_level.value,
                alert.status,
                _dump_json(alert),
                _dump_datetime(alert.created_at),
                _dump_optional_datetime(alert.resolved_at),
            ),
        )
        return alert

    def list_alerts(self, subject_id: str) -> list[RadarAlertRecord]:
        return self._list_json_models(
            """
            SELECT alert_json FROM radar_alerts
            WHERE subject_id = ?
            ORDER BY created_at, alert_id
            """,
            (subject_id,),
            RadarAlertRecord,
        )

    def save_report_artifact(
        self,
        artifact: RoleReportArtifact,
        version: RadarReportArtifactVersion,
    ) -> RoleReportArtifact:
        self._execute(
            """
            INSERT INTO radar_report_artifacts (
              artifact_id, task_id, role, title, generation_mode, current_version_id,
              artifact_json, generated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              title = excluded.title,
              generation_mode = excluded.generation_mode,
              current_version_id = excluded.current_version_id,
              artifact_json = excluded.artifact_json,
              updated_at = excluded.updated_at
            """,
            (
                artifact.artifact_id,
                artifact.task_id,
                artifact.role,
                artifact.title,
                artifact.generation_mode,
                version.artifact_version_id,
                _dump_json(artifact),
                _dump_datetime(artifact.generated_at),
                _dump_datetime(version.created_at),
            ),
        )
        self.save_report_artifact_version(version)
        return artifact

    def get_report_artifact(self, artifact_id: str) -> RoleReportArtifact:
        return self._get_json_model(
            "SELECT artifact_json FROM radar_report_artifacts WHERE artifact_id = ?",
            (artifact_id,),
            RoleReportArtifact,
            f"radar report artifact not found: {artifact_id}",
        )

    def save_report_artifact_version(
        self,
        version: RadarReportArtifactVersion,
    ) -> RadarReportArtifactVersion:
        self._execute(
            """
            INSERT INTO radar_report_artifact_versions (
              artifact_version_id, artifact_id, version_number, source_claim_ids,
              prompt_version, model_provider, model_id, generation_mode,
              version_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_version_id) DO UPDATE SET
              version_json = excluded.version_json
            """,
            (
                version.artifact_version_id,
                version.artifact_id,
                version.version_number,
                _dump_plain_json(version.source_claim_ids),
                version.prompt_version,
                version.model_provider,
                version.model_id,
                version.generation_mode,
                _dump_json(version),
                _dump_datetime(version.created_at),
            ),
        )
        return version

    def list_report_artifact_versions(
        self,
        artifact_id: str,
    ) -> list[RadarReportArtifactVersion]:
        return self._list_json_models(
            """
            SELECT version_json FROM radar_report_artifact_versions
            WHERE artifact_id = ?
            ORDER BY version_number
            """,
            (artifact_id,),
            RadarReportArtifactVersion,
        )

    def save_audit_log(self, entry: RadarAuditLogEntry) -> RadarAuditLogEntry:
        self._execute(
            """
            INSERT INTO radar_audit_logs (
              audit_id, task_id, actor, action, audit_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET audit_json = excluded.audit_json
            """,
            (
                entry.audit_id,
                entry.task_id,
                entry.actor,
                entry.action,
                _dump_json(entry),
                _dump_datetime(entry.created_at),
            ),
        )
        return entry

    def list_audit_logs(self, task_id: str) -> list[RadarAuditLogEntry]:
        return self._list_json_models(
            """
            SELECT audit_json FROM radar_audit_logs
            WHERE task_id = ?
            ORDER BY created_at, audit_id
            """,
            (task_id,),
            RadarAuditLogEntry,
        )

    def save_memory_summary(self, memory: RadarMemorySummary) -> RadarMemorySummary:
        self._execute(
            """
            INSERT INTO radar_memory_summaries (
              memory_summary_id, subject_id, task_id, memory_type, memory_json, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_summary_id) DO UPDATE SET
              memory_json = excluded.memory_json,
              generated_at = excluded.generated_at
            """,
            (
                memory.memory_summary_id,
                memory.subject_id,
                memory.task_id,
                memory.memory_type,
                _dump_json(memory),
                _dump_datetime(memory.generated_at),
            ),
        )
        return memory

    def list_memory_summaries(self, subject_id: str) -> list[RadarMemorySummary]:
        return self._list_json_models(
            """
            SELECT memory_json FROM radar_memory_summaries
            WHERE subject_id = ?
            ORDER BY generated_at, memory_summary_id
            """,
            (subject_id,),
            RadarMemorySummary,
        )

    def load_product_context_state(
        self,
        *,
        context_kind: str,
        subject_id: str,
    ) -> tuple[int, str] | None:
        table = _product_context_table(context_kind)
        row = self._fetchone(
            f"""
            SELECT version, state_json
            FROM {table}
            WHERE subject_id = ?
            """,
            (subject_id,),
        )
        if row is None:
            return None
        return int(row[0]), _database_json_text(row[1])

    def compare_and_set_product_context_state(
        self,
        *,
        context_kind: str,
        subject_id: str,
        expected_version: int,
        next_version: int,
        state_json: str,
        updated_at: datetime,
    ) -> bool:
        """Persist one Product context revision with database-level CAS."""

        table = _product_context_table(context_kind)
        with self._lock:
            cursor = self.connection.cursor()
            try:
                if expected_version == 0:
                    cursor.execute(
                        self._sql(
                            f"""
                            INSERT INTO {table} (
                              subject_id, version, state_json, updated_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(subject_id) DO NOTHING
                            """
                        ),
                        (
                            subject_id,
                            next_version,
                            state_json,
                            _dump_datetime(updated_at),
                        ),
                    )
                else:
                    cursor.execute(
                        self._sql(
                            f"""
                            UPDATE {table}
                            SET version = ?, state_json = ?, updated_at = ?
                            WHERE subject_id = ? AND version = ?
                            """
                        ),
                        (
                            next_version,
                            state_json,
                            _dump_datetime(updated_at),
                            subject_id,
                            expected_version,
                        ),
                    )
                changed = cursor.rowcount == 1
                self.connection.commit()
                return changed
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def load_product_commit_journal_entry(
        self,
        idempotency_key: str,
    ) -> str | None:
        row = self._fetchone(
            """
            SELECT entry_json
            FROM product_commit_journal
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        return None if row is None else _database_json_text(row[0])

    def reserve_product_commit_journal_entry(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        input_hash: str,
        fact_snapshot_hash: str,
        entry_json: str,
        created_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_commit_journal (
                          idempotency_key, tool_name, input_hash,
                          fact_snapshot_hash, state, entry_json,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(idempotency_key) DO NOTHING
                        """
                    ),
                    (
                        idempotency_key,
                        tool_name,
                        input_hash,
                        fact_snapshot_hash,
                        "pending",
                        entry_json,
                        _dump_datetime(created_at),
                        _dump_datetime(created_at),
                    ),
                )
                changed = cursor.rowcount == 1
                self.connection.commit()
                return changed
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def finalize_product_commit_journal_entry(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        input_hash: str,
        fact_snapshot_hash: str,
        entry_json: str,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_commit_journal
                        SET state = ?, entry_json = ?, updated_at = ?
                        WHERE idempotency_key = ?
                          AND tool_name = ?
                          AND input_hash = ?
                          AND fact_snapshot_hash = ?
                          AND state = ?
                        """
                    ),
                    (
                        "final",
                        entry_json,
                        _dump_datetime(updated_at),
                        idempotency_key,
                        tool_name,
                        input_hash,
                        fact_snapshot_hash,
                        "pending",
                    ),
                )
                changed = cursor.rowcount == 1
                self.connection.commit()
                return changed
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def append_product_nonterminal_result(
        self,
        *,
        result_id: str,
        episode_id: str,
        result_json: str,
        recorded_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_episode_results (
              result_id, episode_id, result_json, recorded_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(result_id) DO NOTHING
            """,
            (
                result_id,
                episode_id,
                result_json,
                _dump_datetime(recorded_at),
            ),
        )

    def append_product_episode_result(self, **_kwargs: Any) -> None:
        raise ValueError(
            "legacy Product result writer is fenced; use the terminal bundle "
            "or explicitly nonterminal writer"
        )

    def list_product_episode_result_json(self, episode_id: str) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT result_json
                FROM product_episode_results
                WHERE episode_id = ?
                ORDER BY recorded_at, result_id
                """,
                (episode_id,),
            )
        ]

    def list_all_product_nonterminal_result_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT result_json
                FROM product_episode_results
                ORDER BY recorded_at, result_id
                """
            )
        ]

    def append_product_terminal_bundle(
        self,
        *,
        terminal_result_id: str,
        episode_id: str,
        receipt_revision: int,
        subject_id: str,
        source_result_hash: str,
        result_json: str,
        terminal_recorded_at: datetime,
        manifest_id: str,
        manifest_hash: str,
        ciphertext: str,
        nonce: str,
        wrapped_data_key: str,
        key_id: str,
        manifest_expires_at: datetime,
        job_id: str,
        job_idempotency_key: str,
        job_state: str,
        job_json: str,
        job_event_id: str,
        job_event_json: str,
        operational_state_json: str,
    ) -> bool:
        """Atomically persist Result + encrypted Manifest + Job projection."""

        with self._lock:
            cursor = self.connection.cursor()
            try:
                existing = cursor.execute(
                    self._sql(
                        """
                        SELECT source_result_hash
                        FROM product_episode_result_revisions
                        WHERE episode_id = ? AND receipt_revision = ?
                        """
                    ),
                    (episode_id, receipt_revision),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != source_result_hash:
                        raise ValueError(
                            "episode receipt revision already binds different content"
                        )
                    self.connection.rollback()
                    return False
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_episode_result_revisions (
                          terminal_result_id, episode_id, receipt_revision,
                          subject_id, source_result_hash, result_json,
                          terminal_recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        terminal_result_id,
                        episode_id,
                        receipt_revision,
                        subject_id,
                        source_result_hash,
                        result_json,
                        _dump_datetime(terminal_recorded_at),
                    ),
                )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_induction_job_events (
                          event_id, job_id, event_json, created_at
                        ) VALUES (?, ?, ?, ?)
                        """
                    ),
                    (
                        job_event_id,
                        job_id,
                        job_event_json,
                        _dump_datetime(terminal_recorded_at),
                    ),
                )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_induction_jobs (
                          job_id, idempotency_key, episode_id,
                          terminal_result_id, state, attempt_count,
                          processing_generation, lease_owner,
                          lease_expires_at, next_attempt_at,
                          job_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        job_id,
                        job_idempotency_key,
                        episode_id,
                        terminal_result_id,
                        job_state,
                        0,
                        1,
                        None,
                        None,
                        _dump_datetime(terminal_recorded_at),
                        job_json,
                        _dump_datetime(terminal_recorded_at),
                    ),
                )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_induction_manifests (
                          manifest_id, manifest_hash, subject_id,
                          terminal_result_id, ciphertext, nonce,
                          wrapped_data_key, key_id, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        manifest_id,
                        manifest_hash,
                        subject_id,
                        terminal_result_id,
                        ciphertext,
                        nonce,
                        wrapped_data_key,
                        key_id,
                        _dump_datetime(manifest_expires_at),
                    ),
                )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_longitudinal_operational_state (
                          store_id, state_json, updated_at
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(store_id) DO UPDATE SET
                          state_json = excluded.state_json,
                          updated_at = excluded.updated_at
                        """
                    ),
                    (
                        "canonical",
                        operational_state_json,
                        _dump_datetime(terminal_recorded_at),
                    ),
                )
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def save_product_longitudinal_operational_state(
        self,
        state_json: str,
        *,
        updated_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_longitudinal_operational_state (
              store_id, state_json, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(store_id) DO UPDATE SET
              state_json = excluded.state_json,
              updated_at = excluded.updated_at
            """,
            ("canonical", state_json, _dump_datetime(updated_at)),
        )

    def bootstrap_product_longitudinal_authority(
        self,
        *,
        retrieval_policy_epoch: int,
        digest_read_enabled: bool,
        subject_epochs: list[dict[str, Any]],
        updated_at: datetime,
    ) -> None:
        """Import legacy state once; authority lives in normalized rows thereafter."""

        with self._lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_longitudinal_control (
                          store_id, retrieval_policy_epoch,
                          digest_read_enabled, control_revision, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(store_id) DO NOTHING
                        """
                    ),
                    (
                        "canonical",
                        retrieval_policy_epoch,
                        digest_read_enabled,
                        0,
                        _dump_datetime(updated_at),
                    ),
                )
                for item in subject_epochs:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_longitudinal_subject_epochs (
                              subject_id, privacy_epoch, authorization_epoch,
                              state_revision, updated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(subject_id) DO NOTHING
                            """
                        ),
                        (
                            item["subject_id"],
                            item["privacy_epoch"],
                            item["authorization_epoch"],
                            0,
                            _dump_datetime(updated_at),
                        ),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def load_product_longitudinal_control(self) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT retrieval_policy_epoch, digest_read_enabled, control_revision
            FROM product_longitudinal_control
            WHERE store_id = ?
            """,
            ("canonical",),
        )
        if row is None:
            self.bootstrap_product_longitudinal_authority(
                retrieval_policy_epoch=0,
                digest_read_enabled=False,
                subject_epochs=[],
                updated_at=datetime.now(timezone.utc),
            )
            return self.load_product_longitudinal_control()
        return {
            "retrieval_policy_epoch": int(row[0]),
            "digest_read_enabled": bool(row[1]),
            "control_revision": int(row[2]),
        }

    def load_product_longitudinal_subject_epochs(
        self,
        subject_id: str,
    ) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT privacy_epoch, authorization_epoch, state_revision
            FROM product_longitudinal_subject_epochs
            WHERE subject_id = ?
            """,
            (subject_id,),
        )
        if row is None:
            self.bootstrap_product_longitudinal_authority(
                retrieval_policy_epoch=0,
                digest_read_enabled=False,
                subject_epochs=[
                    {
                        "subject_id": subject_id,
                        "privacy_epoch": 0,
                        "authorization_epoch": 0,
                    }
                ],
                updated_at=datetime.now(timezone.utc),
            )
            return self.load_product_longitudinal_subject_epochs(subject_id)
        return {
            "subject_id": subject_id,
            "privacy_epoch": int(row[0]),
            "authorization_epoch": int(row[1]),
            "state_revision": int(row[2]),
        }

    def list_product_longitudinal_subject_epochs(self) -> list[dict[str, Any]]:
        return [
            {
                "subject_id": str(row[0]),
                "privacy_epoch": int(row[1]),
                "authorization_epoch": int(row[2]),
                "state_revision": int(row[3]),
            }
            for row in self._fetchall(
                """
                SELECT subject_id, privacy_epoch, authorization_epoch,
                       state_revision
                FROM product_longitudinal_subject_epochs
                ORDER BY subject_id
                """
            )
        ]

    def compare_and_set_product_longitudinal_control(
        self,
        *,
        expected_retrieval_policy_epoch: int,
        expected_control_revision: int,
        next_retrieval_policy_epoch: int,
        digest_read_enabled: bool,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_longitudinal_control
                        SET retrieval_policy_epoch = ?,
                            digest_read_enabled = ?,
                            control_revision = control_revision + 1,
                            updated_at = ?
                        WHERE store_id = ?
                          AND retrieval_policy_epoch = ?
                          AND control_revision = ?
                        """
                    ),
                    (
                        next_retrieval_policy_epoch,
                        digest_read_enabled,
                        _dump_datetime(updated_at),
                        "canonical",
                        expected_retrieval_policy_epoch,
                        expected_control_revision,
                    ),
                )
                changed = cursor.rowcount == 1
                self.connection.commit()
                return changed
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def latest_product_terminal_revision(self, episode_id: str) -> int:
        row = self._fetchone(
            """
            SELECT MAX(receipt_revision)
            FROM product_episode_result_revisions
            WHERE episode_id = ?
            """,
            (episode_id,),
        )
        return 0 if row is None or row[0] is None else int(row[0])

    def sync_product_longitudinal_records(
        self,
        *,
        state_json: str,
        updated_at: datetime,
        expected_job_lease: dict[str, Any] | None,
        expected_authority: dict[str, Any] | None,
        next_authority: dict[str, Any] | None,
        expected_terminal_revision: dict[str, Any] | None,
        jobs: list[dict[str, Any]],
        job_events: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        digests: list[dict[str, Any]],
        digest_events: list[dict[str, Any]],
        read_receipts: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        skill_outcomes: list[dict[str, Any]],
        offline_outcomes: list[dict[str, Any]],
    ) -> None:
        """Persist immutable records and mutable projections in one transaction."""

        with self._lock:
            cursor = self.connection.cursor()
            try:
                if expected_authority is not None:
                    subject_id = str(expected_authority["subject_id"])
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_longitudinal_control (
                              store_id, retrieval_policy_epoch,
                              digest_read_enabled, control_revision, updated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(store_id) DO NOTHING
                            """
                        ),
                        ("canonical", 0, False, 0, _dump_datetime(updated_at)),
                    )
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_longitudinal_subject_epochs (
                              subject_id, privacy_epoch, authorization_epoch,
                              state_revision, updated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(subject_id) DO NOTHING
                            """
                        ),
                        (subject_id, 0, 0, 0, _dump_datetime(updated_at)),
                    )
                    cursor.execute(
                        self._sql(
                            """
                            SELECT privacy_epoch, authorization_epoch,
                                   state_revision
                            FROM product_longitudinal_subject_epochs
                            WHERE subject_id = ?
                            """
                        ),
                        (subject_id,),
                    )
                    subject_row = cursor.fetchone()
                    cursor.execute(
                        self._sql(
                            """
                            SELECT retrieval_policy_epoch
                            FROM product_longitudinal_control
                            WHERE store_id = ?
                            """
                        ),
                        ("canonical",),
                    )
                    control_row = cursor.fetchone()
                    if (
                        subject_row is None
                        or control_row is None
                        or int(subject_row[0])
                        != int(expected_authority["privacy_epoch"])
                        or int(subject_row[1])
                        != int(expected_authority["authorization_epoch"])
                        or int(control_row[0])
                        != int(expected_authority["retrieval_policy_epoch"])
                        or (
                            "state_revision" in expected_authority
                            and int(subject_row[2])
                            != int(expected_authority["state_revision"])
                        )
                    ):
                        raise ValueError(
                            "longitudinal authority CAS failed before atomic sync"
                        )
                    if next_authority is not None:
                        cursor.execute(
                            self._sql(
                                """
                                UPDATE product_longitudinal_subject_epochs
                                SET privacy_epoch = ?,
                                    authorization_epoch = ?,
                                    state_revision = state_revision + 1,
                                    updated_at = ?
                                WHERE subject_id = ?
                                  AND privacy_epoch = ?
                                  AND authorization_epoch = ?
                                  AND state_revision = ?
                                """
                            ),
                            (
                                next_authority["privacy_epoch"],
                                next_authority["authorization_epoch"],
                                _dump_datetime(updated_at),
                                subject_id,
                                expected_authority["privacy_epoch"],
                                expected_authority["authorization_epoch"],
                                subject_row[2],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError(
                                "longitudinal subject epoch CAS failed"
                            )
                if expected_terminal_revision is not None:
                    cursor.execute(
                        self._sql(
                            """
                            SELECT MAX(receipt_revision)
                            FROM product_episode_result_revisions
                            WHERE episode_id = ?
                            """
                        ),
                        (expected_terminal_revision["episode_id"],),
                    )
                    row = cursor.fetchone()
                    current_revision = (
                        0 if row is None or row[0] is None else int(row[0])
                    )
                    if current_revision != int(
                        expected_terminal_revision["receipt_revision"]
                    ):
                        raise ValueError(
                            "terminal revision changed before induction commit"
                        )
                expected_lease_committed = expected_job_lease is None
                for item in jobs:
                    if (
                        expected_job_lease is not None
                        and item["job_id"] == expected_job_lease["job_id"]
                    ):
                        cursor.execute(
                            self._sql(
                                """
                                UPDATE product_induction_jobs
                                SET state = ?, attempt_count = ?,
                                    processing_generation = ?,
                                    lease_owner = ?, lease_expires_at = ?,
                                    next_attempt_at = ?, job_json = ?,
                                    updated_at = ?
                                WHERE job_id = ? AND state = ?
                                  AND lease_owner = ? AND attempt_count = ?
                                  AND processing_generation = ?
                                  AND lease_expires_at > ?
                                """
                            ),
                            (
                                item["state"],
                                item["attempt_count"],
                                item["processing_generation"],
                                item["lease_owner"],
                                item["lease_expires_at"],
                                item["next_attempt_at"],
                                item["json"],
                                item["updated_at"],
                                item["job_id"],
                                "leased",
                                expected_job_lease["lease_owner"],
                                expected_job_lease["attempt_count"],
                                expected_job_lease["processing_generation"],
                                expected_job_lease["as_of"],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError(
                                "durable Induction lease changed before atomic sync"
                            )
                        expected_lease_committed = True
                        continue
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_induction_jobs (
                              job_id, idempotency_key, episode_id,
                              terminal_result_id, state, attempt_count,
                              processing_generation, lease_owner,
                              lease_expires_at, next_attempt_at,
                              job_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(job_id) DO UPDATE SET
                              state = excluded.state,
                              attempt_count = excluded.attempt_count,
                              processing_generation = excluded.processing_generation,
                              lease_owner = excluded.lease_owner,
                              lease_expires_at = excluded.lease_expires_at,
                              next_attempt_at = excluded.next_attempt_at,
                              job_json = excluded.job_json,
                              updated_at = excluded.updated_at
                            WHERE
                              excluded.processing_generation
                                > product_induction_jobs.processing_generation
                              OR (
                                excluded.processing_generation
                                  = product_induction_jobs.processing_generation
                                AND excluded.attempt_count
                                  > product_induction_jobs.attempt_count
                              )
                              OR (
                                excluded.processing_generation
                                  = product_induction_jobs.processing_generation
                                AND excluded.attempt_count
                                  = product_induction_jobs.attempt_count
                                AND excluded.state
                                  = product_induction_jobs.state
                                AND product_induction_jobs.updated_at
                                  <= excluded.updated_at
                              )
                            """
                        ),
                        (
                            item["job_id"],
                            item["idempotency_key"],
                            item["episode_id"],
                            item["terminal_result_id"],
                            item["state"],
                            item["attempt_count"],
                            item["processing_generation"],
                            item["lease_owner"],
                            item["lease_expires_at"],
                            item["next_attempt_at"],
                            item["json"],
                            item["updated_at"],
                        ),
                    )
                if not expected_lease_committed:
                    raise ValueError(
                        "expected Induction lease was absent from atomic sync"
                    )
                for item in job_events:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_induction_job_events (
                              event_id, job_id, event_json, created_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(event_id) DO NOTHING
                            """
                        ),
                        (
                            item["event_id"],
                            item["job_id"],
                            item["json"],
                            item["created_at"],
                        ),
                    )
                for item in receipts:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_induction_receipts (
                              receipt_id, job_id, processing_generation,
                              status, receipt_json, completed_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(receipt_id) DO NOTHING
                            """
                        ),
                        (
                            item["receipt_id"],
                            item["job_id"],
                            item["processing_generation"],
                            item["status"],
                            item["json"],
                            item["completed_at"],
                        ),
                    )
                for item in digests:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_episode_digests (
                              digest_id, digest_hash, subject_id, episode_id,
                              source_receipt_revision, digest_json, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(digest_id) DO NOTHING
                            """
                        ),
                        (
                            item["digest_id"],
                            item["digest_hash"],
                            item["subject_id"],
                            item["episode_id"],
                            item["source_receipt_revision"],
                            item["json"],
                            item["expires_at"],
                        ),
                    )
                for item in digest_events:
                    cursor.execute(
                        self._sql(
                            """
                            SELECT event_json
                            FROM product_episode_digest_status_events
                            WHERE event_id = ?
                            """
                        ),
                        (item["event_id"],),
                    )
                    existing_event = cursor.fetchone()
                    if existing_event is not None:
                        if json.loads(_database_json_text(existing_event[0])) != (
                            json.loads(item["json"])
                        ):
                            raise ValueError(
                                "Digest event ID binds different content"
                            )
                        continue
                    cursor.execute(
                        self._sql(
                            """
                            SELECT status_sequence, event_json
                            FROM product_episode_digest_status_events
                            WHERE digest_id = ?
                            ORDER BY status_sequence DESC
                            LIMIT 1
                            """
                        ),
                        (item["digest_id"],),
                    )
                    prior_event = cursor.fetchone()
                    incoming = json.loads(item["json"])
                    prior_sequence = 0 if prior_event is None else int(prior_event[0])
                    prior_status = (
                        None
                        if prior_event is None
                        else json.loads(
                            _database_json_text(prior_event[1])
                        )["to_status"]
                    )
                    if (
                        int(item["status_sequence"]) != prior_sequence + 1
                        or incoming.get("from_status") != prior_status
                    ):
                        raise ValueError(
                            "Digest status sequence CAS failed"
                        )
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_episode_digest_status_events (
                              event_id, digest_id, status_sequence,
                              event_json, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(event_id) DO NOTHING
                            """
                        ),
                        (
                            item["event_id"],
                            item["digest_id"],
                            item["status_sequence"],
                            item["json"],
                            item["created_at"],
                        ),
                    )
                for item in read_receipts:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_memory_read_receipts (
                              receipt_id, subject_id, receipt_json, completed_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(receipt_id) DO NOTHING
                            """
                        ),
                        (
                            item["receipt_id"],
                            item["subject_id"],
                            item["json"],
                            item["completed_at"],
                        ),
                    )
                for item in candidates:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_pending_profile_candidates (
                              candidate_hash, subject_id, status,
                              candidate_json, expires_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(subject_id, candidate_hash) DO UPDATE SET
                              status = CASE
                                WHEN product_pending_profile_candidates.status
                                  = 'pending'
                                THEN excluded.status
                                ELSE product_pending_profile_candidates.status
                              END,
                              candidate_json = CASE
                                WHEN product_pending_profile_candidates.status
                                  = 'pending'
                                THEN excluded.candidate_json
                                ELSE product_pending_profile_candidates.candidate_json
                              END
                            """
                        ),
                        (
                            item["candidate_hash"],
                            item["subject_id"],
                            item["status"],
                            item["json"],
                            item["expires_at"],
                        ),
                    )
                for item in skill_outcomes:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_skill_outcomes (
                              outcome_id, episode_id, status,
                              outcome_json, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(outcome_id) DO UPDATE SET
                              status = CASE
                                WHEN product_skill_outcomes.status = 'superseded'
                                THEN product_skill_outcomes.status
                                ELSE excluded.status
                              END,
                              outcome_json = CASE
                                WHEN product_skill_outcomes.status = 'superseded'
                                THEN product_skill_outcomes.outcome_json
                                ELSE excluded.outcome_json
                              END
                            """
                        ),
                        (
                            item["outcome_id"],
                            item["episode_id"],
                            item["status"],
                            item["json"],
                            item["created_at"],
                        ),
                    )
                for item in offline_outcomes:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_offline_skill_outcomes (
                              envelope_id, subject_id, source_result_hash,
                              withdrawn, envelope_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(envelope_id) DO UPDATE SET
                              withdrawn = CASE
                                WHEN product_offline_skill_outcomes.withdrawn
                                THEN product_offline_skill_outcomes.withdrawn
                                ELSE excluded.withdrawn
                              END,
                              envelope_json = CASE
                                WHEN product_offline_skill_outcomes.withdrawn
                                THEN product_offline_skill_outcomes.envelope_json
                                ELSE excluded.envelope_json
                              END,
                              updated_at = excluded.updated_at
                            """
                        ),
                        (
                            item["envelope_id"],
                            item["subject_id"],
                            item["source_result_hash"],
                            item["withdrawn"],
                            item["json"],
                            item["updated_at"],
                        ),
                    )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_longitudinal_operational_state (
                          store_id, state_json, updated_at
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(store_id) DO UPDATE SET
                          state_json = excluded.state_json,
                          updated_at = excluded.updated_at
                        """
                    ),
                    ("canonical", state_json, _dump_datetime(updated_at)),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def load_product_longitudinal_operational_state(self) -> str | None:
        row = self._fetchone(
            """
            SELECT state_json
            FROM product_longitudinal_operational_state
            WHERE store_id = ?
            """,
            ("canonical",),
        )
        return None if row is None else _database_json_text(row[0])

    def list_product_terminal_result_rows(self) -> list[tuple[str, str]]:
        return [
            (str(row[0]), _database_json_text(row[1]))
            for row in self._fetchall(
                """
                SELECT terminal_result_id, result_json
                FROM product_episode_result_revisions
                ORDER BY terminal_recorded_at, terminal_result_id
                """
            )
        ]

    def count_product_terminal_orphans(self) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*)
            FROM product_episode_result_revisions AS results
            LEFT JOIN product_induction_manifests AS manifests
              ON manifests.terminal_result_id = results.terminal_result_id
            LEFT JOIN product_induction_jobs AS jobs
              ON jobs.terminal_result_id = results.terminal_result_id
            WHERE manifests.manifest_id IS NULL OR jobs.job_id IS NULL
            """
        )
        return 0 if row is None else int(row[0])

    def compare_and_set_product_induction_lease(
        self,
        *,
        job_id: str,
        expected_attempt_count: int,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        updated_job_json: str,
        event_id: str,
        event_json: str,
    ) -> bool:
        """Cross-process lease CAS over the durable Job projection."""

        with self._lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_induction_jobs
                        SET state = ?, attempt_count = ?,
                            lease_owner = ?, lease_expires_at = ?,
                            job_json = ?, updated_at = ?
                        WHERE job_id = ?
                          AND attempt_count = ?
                          AND (
                            (
                              state IN (?, ?)
                              AND next_attempt_at <= ?
                            )
                            OR (
                              state = ?
                              AND lease_expires_at IS NOT NULL
                              AND lease_expires_at <= ?
                            )
                          )
                        """
                    ),
                    (
                        "leased",
                        expected_attempt_count + 1,
                        worker_id,
                        _dump_datetime(lease_expires_at),
                        updated_job_json,
                        _dump_datetime(now),
                        job_id,
                        expected_attempt_count,
                        "pending",
                        "retryable_failed",
                        _dump_datetime(now),
                        "leased",
                        _dump_datetime(now),
                    ),
                )
                changed = cursor.rowcount == 1
                if changed:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_induction_job_events (
                              event_id, job_id, event_json, created_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(event_id) DO NOTHING
                            """
                        ),
                        (
                            event_id,
                            job_id,
                            event_json,
                            _dump_datetime(now),
                        ),
                    )
                self.connection.commit()
                return changed
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def product_induction_lease_matches(
        self,
        *,
        job_id: str,
        lease_owner: str,
        attempt_count: int,
        now: datetime,
    ) -> bool:
        row = self._fetchone(
            """
            SELECT 1
            FROM product_induction_jobs
            WHERE job_id = ? AND state = ?
              AND lease_owner = ? AND attempt_count = ?
              AND lease_expires_at > ?
            """,
            (
                job_id,
                "leased",
                lease_owner,
                attempt_count,
                _dump_datetime(now),
            ),
        )
        return row is not None

    def list_product_induction_job_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT job_json
                FROM product_induction_jobs
                ORDER BY updated_at, job_id
                """
            )
        ]

    def list_product_induction_job_event_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT event_json
                FROM product_induction_job_events
                ORDER BY created_at, event_id
                """
            )
        ]

    def list_product_induction_receipt_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT receipt_json
                FROM product_induction_receipts
                ORDER BY completed_at, receipt_id
                """
            )
        ]

    def list_product_episode_digest_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT digest_json
                FROM product_episode_digests
                ORDER BY expires_at, digest_id
                """
            )
        ]

    def list_product_episode_digest_event_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT event_json
                FROM product_episode_digest_status_events
                ORDER BY digest_id, status_sequence
                """
            )
        ]

    def list_product_memory_read_receipt_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT receipt_json
                FROM product_memory_read_receipts
                ORDER BY completed_at, receipt_id
                """
            )
        ]

    def list_product_pending_candidate_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT candidate_json
                FROM product_pending_profile_candidates
                ORDER BY subject_id, candidate_hash
                """
            )
        ]

    def list_product_skill_outcome_json(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT outcome_json
                FROM product_skill_outcomes
                ORDER BY created_at, outcome_id
                """
            )
        ]

    def list_product_offline_skill_outcome_rows(
        self,
    ) -> list[tuple[str, str, str]]:
        return [
            (
                str(row[0]),
                str(row[1]),
                _database_json_text(row[2]),
            )
            for row in self._fetchall(
                """
                SELECT subject_id, source_result_hash, envelope_json
                FROM product_offline_skill_outcomes
                ORDER BY updated_at, envelope_id
                """
            )
        ]

    def load_product_manifest_envelope(
        self,
        manifest_id: str,
    ) -> tuple[str, str, str, str] | None:
        row = self._fetchone(
            """
            SELECT ciphertext, nonce, wrapped_data_key, key_id
            FROM product_induction_manifests
            WHERE manifest_id = ? AND purged_at IS NULL
            """,
            (manifest_id,),
        )
        if row is None or any(value is None for value in row):
            return None
        return tuple(str(value) for value in row)  # type: ignore[return-value]

    def purge_product_manifest_envelope(
        self,
        *,
        manifest_id: str,
        purge_receipt_ref: str,
        purged_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                self._sql(
                    """
                    UPDATE product_induction_manifests
                    SET ciphertext = NULL, nonce = NULL,
                        wrapped_data_key = NULL, key_id = NULL,
                        purged_at = ?, purge_receipt_ref = ?
                    WHERE manifest_id = ? AND purged_at IS NULL
                    """
                ),
                (
                    _dump_datetime(purged_at),
                    purge_receipt_ref,
                    manifest_id,
                ),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def save_product_publication_entry(
        self,
        *,
        intent_id: str,
        episode_id: str,
        draft_hash: str,
        state: str,
        entry_json: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO product_publication_journal (
              intent_id, episode_id, draft_hash, state, entry_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
              state = excluded.state,
              entry_json = excluded.entry_json,
              updated_at = excluded.updated_at
            """,
            (
                intent_id,
                episode_id,
                draft_hash,
                state,
                entry_json,
                _dump_datetime(created_at),
                _dump_datetime(updated_at),
            ),
        )

    def reserve_product_publication_entry(
        self,
        *,
        intent_id: str,
        episode_id: str,
        draft_hash: str,
        entry_json: str,
        created_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                self._sql(
                    """
                    INSERT INTO product_publication_journal (
                      intent_id, episode_id, draft_hash, state, entry_json,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intent_id) DO NOTHING
                    """
                ),
                (
                    intent_id,
                    episode_id,
                    draft_hash,
                    "reserved",
                    entry_json,
                    _dump_datetime(created_at),
                    _dump_datetime(created_at),
                ),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def load_product_publication_entry(self, intent_id: str) -> str | None:
        row = self._fetchone(
            """
            SELECT entry_json
            FROM product_publication_journal
            WHERE intent_id = ?
            """,
            (intent_id,),
        )
        return None if row is None else _database_json_text(row[0])

    def finalize_product_publication_entry(
        self,
        *,
        intent_id: str,
        delivered: bool,
        entry_json: str,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                self._sql(
                    """
                    UPDATE product_publication_journal
                    SET state = ?, entry_json = ?, updated_at = ?
                    WHERE intent_id = ? AND state = ?
                    """
                ),
                (
                    "delivered" if delivered else "failed",
                    entry_json,
                    _dump_datetime(updated_at),
                    intent_id,
                    "reserved",
                ),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def load_product_publication_entries(self) -> list[str]:
        return [
            _database_json_text(row[0])
            for row in self._fetchall(
                """
                SELECT entry_json
                FROM product_publication_journal
                ORDER BY created_at, intent_id
                """
            )
        ]

    def _get_json_model(
        self,
        sql: str,
        params: tuple[Any, ...],
        model: type[Any],
        error_message: str,
    ) -> Any:
        row = self._fetchone(sql, params)
        if row is None:
            raise KeyError(error_message)
        return model.model_validate_json(row[0])

    def _list_json_models(
        self,
        sql: str,
        params: tuple[Any, ...],
        model: type[Any],
    ) -> list[Any]:
        return [model.model_validate_json(row[0]) for row in self._fetchall(sql, params)]

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self.connection.execute(self._sql(sql), params)
            self.connection.commit()

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        with self._lock:
            return self.connection.execute(self._sql(sql), params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            return list(self.connection.execute(self._sql(sql), params).fetchall())

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql


def connect_postgres_store(database_url: str) -> RadarPersistenceStore:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised only without extra.
        raise RuntimeError("psycopg is required for radar PostgreSQL persistence.") from exc
    connection = psycopg.connect(database_url)
    with connection.cursor() as cursor:
        # Bootstrap only the migration ledger, then apply each version once.  This
        # keeps ALTER-based additive migrations safe across process restarts.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_agent_schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute("SELECT version FROM radar_agent_schema_migrations")
        applied = {str(row[0]) for row in cursor.fetchall()}
        for version in MIGRATION_VERSIONS:
            if version in applied:
                continue
            for statement in split_sql_statements(
                RADAR_AGENT_POSTGRES_MIGRATIONS[version]
            ):
                cursor.execute(statement)
    connection.commit()
    return RadarPersistenceStore(connection, dialect="postgres")


def _dump_json(model: Any) -> str:
    return model.model_dump_json()


def _dump_plain_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _database_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _dump_datetime(value: datetime) -> str:
    return value.isoformat()


def _dump_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _dump_datetime(value)


def _night_summary_id(radar_device_id: str, night_of: date) -> str:
    return f"{radar_device_id}:{night_of.isoformat()}"


def _product_context_table(context_kind: str) -> str:
    try:
        return {
            "care": "product_care_context_states",
            "memory": "product_memory_context_states",
        }[context_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported Product context kind: {context_kind}") from exc


__all__ = [
    "RadarPersistenceStore",
    "connect_postgres_store",
]
