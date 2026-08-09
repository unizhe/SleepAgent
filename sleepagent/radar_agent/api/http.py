from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field, model_validator

from sleepagent.product_device import PRODUCT_RADAR_API_KEY_ENV
from sleepagent.radar_agent.boundary import RADAR_AGENT_API_PREFIX
from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    RadarSubject,
    connect_postgres_store,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider, SUPPORTED_REPLAY_SCENARIOS
from sleepagent.radar_agent.replay import ReplayScenario, get_replay_scenario
from sleepagent.radar_agent.runtime import (
    AuthorizationRequired,
    BindingMismatch,
    IdempotencyConflict,
    InvalidTaskTransition,
    RadarAgentTask,
    RadarArtifactVersion,
    RadarTaskEvent,
    RadarTaskStatus,
    RoleAccessDenied,
    TaskService,
    UserInputRequest,
    UserInputResponse,
)
from sleepagent.radar_agent.schemas import (
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentSchema,
    RadarNightSummary,
)
from sleepagent.radar_agent.questionnaire import QuestionnaireCandidate
from sleepagent.radar_agent.product_agent import (
    ActionProposal,
    AgentId as ProductAgentId,
    AuthenticatedBinding,
    ClaimKind,
    ConfirmationToken,
    DecisionExplanation,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    HITL_POLICY_VERSION,
    HumanDecisionChoice,
    HumanDecisionError,
    HumanDecisionRequest,
    HumanDecisionService,
    HumanDecisionStatus,
    PendingConfirmationTarget,
    PersistentHumanDecisionRepository,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductEpisodeRunner,
    ProductUserFactResponse,
    SourceScope,
    SourceScopeKind,
    build_unavailable_entry_decisions,
    build_product_episode_runner_from_env,
    product_episode_runner_is_configured,
    stable_hash,
    snapshot_binding_material,
)


RADAR_AGENT_API_KEY_ENV = "SLEEPAGENT_RADAR_AGENT_API_KEY"
RADAR_AGENT_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"
RADAR_AGENT_SQLITE_PATH_ENV = "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH"
DEFAULT_RADAR_AGENT_SQLITE_PATH = "/tmp/sleepagent_radar_agent.sqlite3"
RADAR_AGENT_LLM_BASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_BASE_URL"
RADAR_AGENT_LLM_API_KEY_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_API_KEY"
RADAR_AGENT_LLM_MODEL_ID_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_MODEL_ID"
RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_TIMEOUT_SECONDS"
RADAR_AGENT_LLM_RETRY_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_RETRY"
RADAR_AGENT_DEV_MODE_ENV = "SLEEPAGENT_RADAR_AGENT_DEV_MODE"
DEPLOYMENT_MODE_ENV = "SLEEPAGENT_DEPLOYMENT_MODE"
PRODUCT_EPISODE_CHECKPOINT_ARTIFACT = "_product_episode_checkpoint"


class ProductGoalType(str, Enum):
    NIGHT_REVIEW = "night_review"
    TREND_COMPARISON = "trend_comparison"
    CHANGE_EXPLANATION = "change_explanation"
    DATA_QUALITY_DIAGNOSIS = "data_quality_diagnosis"
    DOCTOR_MATERIAL = "doctor_material"
    GROUNDED_QUESTION = "grounded_question"


class RadarTaskCreateRequest(RadarAgentSchema):
    subject_id: str = Field(default="elder-demo-001", min_length=1)
    radar_device_id: str | None = None
    role: Literal["elder", "family", "doctor"] = "family"
    actor_id: str = Field(default="demo-family-user", min_length=1)
    role_binding_ids: list[str] = Field(default_factory=list)
    authorization_id: str | None = None
    scenario: str = "normal_night"
    provider_input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    max_retries: int = Field(default=3, ge=0, le=10)
    runtime_kind: Literal["product_episode"] = "product_episode"
    goal_type: ProductGoalType | None = None
    target_date: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    question: str | None = Field(default=None, max_length=1200)
    source_artifact_id: str | None = None
    source_date: date | None = None
    focus: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def scenario_is_supported(self) -> "RadarTaskCreateRequest":
        if self.scenario not in SUPPORTED_REPLAY_SCENARIOS:
            raise ValueError(f"Unsupported replay scenario: {self.scenario}.")
        return self


class RadarTaskDetail(RadarAgentSchema):
    task: RadarAgentTask
    risk_level: str | None = None
    replay_scenario: ReplayScenario | None = None
    artifacts: list[RadarArtifactVersion] = Field(default_factory=list)
    confirmations: list[HumanConfirmationRequest] = Field(default_factory=list)
    decisions: list[HumanDecisionRequest] = Field(default_factory=list)
    questionnaire_candidates: list[QuestionnaireCandidate] = Field(
        default_factory=list,
        max_length=3,
    )
    user_input_requests: list[UserInputRequest] = Field(default_factory=list)
    completion_receipt: dict[str, Any] | EpisodeReceipt | None = None


class RadarUserInputAnswerRequest(RadarAgentSchema):
    request_id: str = Field(..., min_length=1)
    answer: str | None = Field(default=None, min_length=1, max_length=1000)
    declined: bool = False

    @model_validator(mode="after")
    def answer_or_decline(self) -> "RadarUserInputAnswerRequest":
        if self.declined == (self.answer is not None):
            raise ValueError("provide exactly one of answer or declined=true")
        return self


class RadarConfirmationResolveRequest(RadarAgentSchema):
    confirmation_id: str = Field(..., min_length=1)
    approved: bool
    actor_id: str = Field(..., min_length=1)
    actor_role: Literal["elder", "family", "doctor", "system"]
    reason: str | None = Field(default=None, max_length=1000)


class RadarDecisionRevokeRequest(RadarAgentSchema):
    reason: str = Field(..., min_length=1, max_length=1000)


class RadarChatRequest(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=4000)
    actor_id: str = Field(..., min_length=1)
    actor_role: Literal["elder", "family", "doctor"] | None = None
    role: Literal["elder", "family", "doctor"] = "family"


class RadarChatResponse(RadarAgentSchema):
    task_id: str
    role: str
    answer: str
    evidence_refs: list[str] = Field(default_factory=list)
    rag_citation_refs: list[str] = Field(default_factory=list)
    questionnaire_ids: list[str] = Field(default_factory=list)
    questionnaire_candidates: list[dict[str, Any]] = Field(default_factory=list)
    boundary_action: str | None = None
    generation_mode: str = "template"
    facts_mutated: bool = False


class RadarApiRuntime:
    """Own the API's single persistent TaskService and its replay adapters."""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        product_runner: ProductEpisodeRunner | None = None,
    ) -> None:
        database_url = os.getenv(RADAR_AGENT_DATABASE_URL_ENV)
        production = (
            os.getenv(DEPLOYMENT_MODE_ENV, "development").strip().lower()
            == "production"
        )
        if connection is not None:
            if production:
                raise RuntimeError(
                    "production Radar Agent API cannot use an injected "
                    "SQLite connection"
                )
            self.store = RadarPersistenceStore.connect_sqlite(connection)
        elif database_url:
            lowered = database_url.strip().lower()
            if production and (
                lowered.startswith("sqlite")
                or ":memory:" in lowered
                or "/tmp/" in lowered
            ):
                raise RuntimeError(
                    "production Radar Agent API requires shared "
                    "PostgreSQL storage"
                )
            self.store = connect_postgres_store(database_url)
        else:
            if production:
                raise RuntimeError(
                    "production Radar Agent API requires "
                    f"{RADAR_AGENT_DATABASE_URL_ENV}"
                )
            sqlite_path = Path(
                os.getenv(
                    RADAR_AGENT_SQLITE_PATH_ENV,
                    DEFAULT_RADAR_AGENT_SQLITE_PATH,
                )
            )
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self.store = RadarPersistenceStore.connect_sqlite(
                sqlite3.connect(sqlite_path, check_same_thread=False)
            )
        # API-key authentication and per-task ownership are enforced at the HTTP
        # boundary. Domain deployments can pre-provision authorization records and
        # replace this runtime without changing the routes.
        self.service = TaskService(
            self.store,
            validate_bindings=False,
            require_authorization=False,
        )
        self._lock = RLock()
        self.product_runner = (
            product_runner
            or build_product_episode_runner_from_env(
                persistence_store=self.store,
            )
        )
        self.human_decisions = HumanDecisionService(
            PersistentHumanDecisionRepository(self.store),
            authority_validator=self._validate_human_decision_authority,
        )

    def _validate_human_decision_authority(
        self,
        request: HumanDecisionRequest,
        actor_id: str,
        actor_role: str,
        role_binding_id: str | None,
        authorization_id: str | None,
    ) -> None:
        if _development_mode_enabled():
            if actor_role not in {item.role for item in request.requirements}:
                raise HumanDecisionError("actor role is not required by this decision")
            return
        if role_binding_id is None:
            raise HumanDecisionError("a role binding is required for this decision")
        bindings = {
            item.role_binding_id: item
            for item in self.store.list_role_bindings(
                request.proposal.subject_id
            )
        }
        binding = bindings.get(role_binding_id)
        if binding is None or (
            binding.user_id != actor_id
            or binding.role != actor_role
            or binding.subject_id != request.proposal.subject_id
        ):
            raise HumanDecisionError("role binding does not authorize this actor")
        if (
            request.proposal.action_kind == "external_action"
            and actor_role == "doctor"
        ):
            required_permission = "read_doctor_material"
            required_scope = "process_supplementary_document"
        elif request.proposal.action_kind == "external_action":
            required_permission = "export_data"
            required_scope = "export_data"
        else:
            required_permission = "process_health_data"
            required_scope = "process_radar_summary"
        if required_permission not in binding.permissions:
            raise HumanDecisionError(
                f"role binding lacks {required_permission!r} permission"
            )
        effective_authorization_id = (
            authorization_id or request.proposal.authorization_id
        )
        if effective_authorization_id is None:
            raise HumanDecisionError("an active data authorization is required")
        authorization = self.store.get_data_authorization(
            effective_authorization_id
        )
        now = datetime.now(timezone.utc)
        if (
            authorization.subject_id != request.proposal.subject_id
            or authorization.status != "active"
            or (
                authorization.expires_at is not None
                and authorization.expires_at <= now
            )
        ):
            raise HumanDecisionError("data authorization is inactive or mismatched")
        if required_scope not in authorization.scopes:
            raise HumanDecisionError(
                f"data authorization lacks {required_scope!r} scope"
            )

    def create_task(
        self,
        payload: RadarTaskCreateRequest,
        *,
        idempotency_key: str | None,
    ) -> RadarAgentTask:
        provider = ReplayRadarProvider(scenario=payload.scenario)
        device = provider.get_device(payload.radar_device_id or provider.list_devices()[0].radar_device_id)
        device = device.model_copy(update={"bound_subject_id": payload.subject_id})
        now = datetime.now(timezone.utc)
        with self._lock:
            self.store.save_subject(
                RadarSubject(
                    subject_id=payload.subject_id,
                    display_name=payload.subject_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            self.store.save_device(device)
            task_service = (
                self.service
                if _development_mode_enabled()
                else TaskService(
                    self.store,
                    validate_bindings=True,
                    require_authorization=True,
                )
            )
            goal_type = payload.goal_type or ProductGoalType.NIGHT_REVIEW
            goal_payload = {
                "goal_type": goal_type.value,
                "target_date": (
                    payload.target_date.isoformat()
                    if payload.target_date
                    else None
                ),
                "range_start": (
                    payload.range_start.isoformat()
                    if payload.range_start
                    else None
                ),
                "range_end": (
                    payload.range_end.isoformat()
                    if payload.range_end
                    else None
                ),
                "question": payload.question,
                "source_artifact_id": payload.source_artifact_id,
                "source_date": (
                    payload.source_date.isoformat()
                    if payload.source_date
                    else None
                ),
                "focus": payload.focus,
                "resolved_timezone_name": device.timezone_name,
                "requested_role": payload.role,
                "requested_outputs": [_goal_output(goal_type)],
            }
            task = task_service.create_task(
                subject_id=payload.subject_id,
                radar_device_id=device.radar_device_id,
                role=payload.role,
                role_binding_ids=payload.role_binding_ids,
                actor_id=payload.actor_id,
                authorization_id=payload.authorization_id,
                scenario=payload.scenario,
                provider_input={"provider": "replay", **payload.provider_input},
                idempotency_key=idempotency_key or payload.idempotency_key,
                max_retries=payload.max_retries,
                runtime_kind="product_episode",
                runtime_contract_version="product-episode.v1",
                goal_payload=goal_payload,
            )
            return task

    def run_task(self, task_id: str) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        _assert_task_runtime_writable(task)
        return self._run_product_task(task)

    def _run_product_task(self, task: RadarAgentTask) -> RadarTaskDetail:
        if task.status == RadarTaskStatus.COMPLETED:
            return self.detail(task.task_id)
        if task.status == RadarTaskStatus.CREATED:
            self.service.transition_task(
                task.task_id,
                RadarTaskStatus.RUNNING,
                message="Product Episode accepted for four-role execution.",
            )
        elif task.status != RadarTaskStatus.RUNNING:
            raise InvalidTaskTransition(
                f"product task in {task.status.value!r} state cannot be run"
            )
        episode_request = self._product_request_for_task(task)
        return self._execute_product_task(task, episode_request)

    def _execute_product_task(
        self,
        task: RadarAgentTask,
        episode_request: ProductEpisodeRunRequest,
        *,
        precomputed_result: ProductEpisodeRunResult | None = None,
    ) -> RadarTaskDetail:
        previous_checkpoint: RadarArtifactVersion | None = None
        try:
            previous_checkpoint = self._latest_product_checkpoint(
                task.task_id
            )
            previous_targets = [
                PendingConfirmationTarget.model_validate(item)
                for item in previous_checkpoint.payload.get(
                    "pending_confirmations",
                    [],
                )
            ]
        except InvalidTaskTransition:
            previous_targets = []
        result = (
            precomputed_result
            if precomputed_result is not None
            else self.product_runner.run(episode_request)
        )
        for invocation in (
            result.agent_invocations if precomputed_result is None else []
        ):
            self.service.emit_event(
                task.task_id,
                event_type="agent.completed",
                message=(
                    f"{invocation.agent_id.value} completed "
                    f"{invocation.skill_id}@{invocation.skill_version}."
                ),
                payload={
                    "agent": invocation.agent_id.value,
                    "invocation_id": invocation.invocation_id,
                    "skill_id": invocation.skill_id,
                    "skill_version": invocation.skill_version,
                    "profile_version": invocation.profile_version,
                    "target_hash": invocation.target_hash,
                    "provider_request_id": invocation.provider_request_id,
                },
            )
        previous_receipt_ids = {
            item.tool_invocation_id
            for item in (
                ProductEpisodeRunResult.model_validate(
                    previous_checkpoint.payload["result"]
                ).tool_receipts
                if precomputed_result is not None
                and previous_checkpoint is not None
                and "result" in previous_checkpoint.payload
                else []
            )
        }
        for receipt in [
            item
            for item in result.tool_receipts
            if item.tool_invocation_id not in previous_receipt_ids
        ]:
            self.service.emit_event(
                task.task_id,
                event_type="tool.completed",
                message=(
                    f"{receipt.tool_name} completed with "
                    f"{receipt.outcome.value}."
                ),
                payload={
                    "tool_name": receipt.tool_name,
                    "tool_invocation_id": receipt.tool_invocation_id,
                    "outcome": receipt.outcome.value,
                    "source_refs": receipt.source_refs,
                },
            )
        self.service.save_payload_artifact(
            task.task_id,
            artifact_id=f"product-episode:{task.task_id}",
            artifact_type="product_episode_result",
            payload={
                "receipt": result.receipt.model_dump(mode="json"),
                "publication": (
                    result.publication.model_dump(mode="json")
                    if result.publication
                    else None
                ),
                "publication_delivered": result.publication_delivered,
                "external_action_delivery_status": (
                    result.external_action_delivery_status
                ),
                "external_action_target_id": result.external_action_target_id,
                "external_action_target_hash": (
                    result.external_action_target_hash
                ),
                "registry_hash": result.registry_hash,
                "agent_invocations": [
                    {
                        "invocation_id": item.invocation_id,
                        "agent_id": item.agent_id.value,
                        "skill_id": item.skill_id,
                        "skill_version": item.skill_version,
                        "target_hash": item.target_hash,
                        "provider": item.provider,
                        "model_id": item.model_id,
                        "provider_request_id": item.provider_request_id,
                    }
                    for item in result.agent_invocations
                ],
                "tool_receipts": [
                    {
                        "tool_invocation_id": item.tool_invocation_id,
                        "tool_name": item.tool_name,
                        "outcome": item.outcome.value,
                        "source_refs": item.source_refs,
                    }
                    for item in result.tool_receipts
                ],
                "pending_confirmations": [
                    item.model_dump(mode="json")
                    for item in result.pending_confirmations
                ],
                "pending_user_input": (
                    result.pending_user_input.model_dump(mode="json")
                    if result.pending_user_input
                    else None
                ),
            },
            source_refs=list(task.provider_input.get("source_refs", [])),
            metadata={
                "runtime_kind": "product_episode",
                "runtime_contract_version": "product-episode.v1",
                "risk_level": get_replay_scenario(
                    task.scenario
                ).expected.risk_level.value,
            },
        )
        self._save_product_checkpoint(
            task=task,
            request=episode_request,
            result=result,
        )
        current = self.service.get_task(task.task_id)
        current = current.model_copy(
            update={
                "execution_mode": result.receipt.execution_mode.value,
                "completion_status": _product_completion_status(
                    result.receipt.status
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.store.save_task(current)
        status = {
            EpisodeStatus.WAITING_USER: RadarTaskStatus.WAITING_FOR_USER_INPUT,
            EpisodeStatus.WAITING_CONFIRMATION: (
                RadarTaskStatus.WAITING_FOR_CONFIRMATION
            ),
            EpisodeStatus.BLOCKED: RadarTaskStatus.FAILED,
        }.get(result.receipt.status, RadarTaskStatus.COMPLETED)
        self.service.transition_task(
            task.task_id,
            status,
            message=(
                "Product Episode finished with "
                f"{result.receipt.status.value}."
            ),
        )
        self._register_product_interactions(
            task=task,
            request=episode_request,
            result=result,
        )
        self._complete_product_confirmation_actions(
            task=task,
            result=result,
            targets=previous_targets or result.pending_confirmations,
        )
        return self.detail(task.task_id)

    def _save_product_checkpoint(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
    ) -> None:
        self.service.save_payload_artifact(
            task.task_id,
            artifact_id=f"product-checkpoint:{task.task_id}",
            artifact_type=PRODUCT_EPISODE_CHECKPOINT_ARTIFACT,
            payload={
                "request": request.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "pending_confirmations": [
                    item.model_dump(mode="json")
                    for item in result.pending_confirmations
                ],
                "pending_user_input": (
                    result.pending_user_input.model_dump(mode="json")
                    if result.pending_user_input
                    else None
                ),
                "receipt_ref": result.receipt.trace_ref,
            },
            source_refs=[result.receipt.fact_snapshot_id],
            metadata={
                "runtime_kind": "product_episode",
                "internal": True,
            },
        )

    def _latest_product_checkpoint(
        self,
        task_id: str,
    ) -> RadarArtifactVersion:
        candidates = [
            item
            for item in self.service.list_artifacts(task_id)
            if item.artifact_type
            == PRODUCT_EPISODE_CHECKPOINT_ARTIFACT
        ]
        if not candidates:
            raise InvalidTaskTransition(
                "product task has no resumable checkpoint"
            )
        return max(candidates, key=lambda item: (item.version, item.created_at))

    def _register_product_interactions(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
    ) -> None:
        for target in result.pending_confirmations:
            decision = self._ensure_product_human_decision(
                task=task,
                request=request,
                result=result,
                target=target,
            )
            confirmation_id = _product_confirmation_id(
                task.task_id,
                target,
            )
            self.service.request_confirmation(
                task.task_id,
                HumanConfirmationRequest(
                    confirmation_id=confirmation_id,
                    task_id=task.task_id,
                    action_type=(
                        f"product_{target.target_kind}:"
                        f"{target.action_scope}"
                    ),
                    requested_role="elder",
                    allowed_roles=[
                        item.role
                        for item in decision.requirements
                        if item.role in {"elder", "family", "doctor", "system"}
                    ],
                    reason=target.reason,
                    evidence_refs=[
                        target.candidate_id,
                        target.candidate_hash,
                    ],
                    idempotency_key=confirmation_id,
                    created_at=datetime.now(timezone.utc),
                ),
            )
        pending = result.pending_user_input
        if pending is None:
            return
        request_id = _product_user_input_id(
            task.task_id,
            pending.request_id,
        )
        try:
            existing = self.store.get_user_input_request(request_id)
        except KeyError:
            existing = None
        if existing is not None:
            if existing.status != "pending":
                raise InvalidTaskTransition(
                    "Product Agent repeated a resolved user-input request"
                )
            return
        self.store.save_user_input_request(
            UserInputRequest(
                request_id=request_id,
                task_id=task.task_id,
                question_id=pending.request_id,
                question_version="product-user-fact.v1",
                question_text=pending.question_text,
                question_type="observable_fact",
                target_role=pending.target_role,
                why_needed=pending.why_needed,
                decision_scope=pending.decision_scope,
                asked_by_agent_invocation_id=None,
                expires_at=pending.expires_at,
            )
        )
        self.service.emit_event(
            task.task_id,
            event_type="user_input.requested",
            message=pending.question_text,
            payload={
                "request_id": request_id,
                "source_agent": pending.source_agent.value,
                "decision_scope": pending.decision_scope,
            },
        )

    def _ensure_product_human_decision(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
        target: PendingConfirmationTarget,
    ) -> HumanDecisionRequest:
        existing = [
            item
            for item in self.human_decisions.repository.list(task_id=task.task_id)
            if item.proposal.target_hash == target.candidate_hash
            and item.proposal.target_id == target.candidate_id
            and item.proposal.action_scope == target.action_scope
        ]
        if existing:
            return existing[-1]
        payload: dict[str, Any] = {
            "candidate_id": target.candidate_id,
            "candidate_hash": target.candidate_hash,
            "target_kind": target.target_kind,
        }
        exact_changes = [
            f"目标类型：{target.target_kind}",
            f"操作范围：{target.action_scope}",
            f"目标版本指纹：{target.candidate_hash[:16]}",
        ]
        if target.target_kind == "memory" and result.publication is not None:
            candidate = next(
                (
                    item
                    for item in result.publication.memory_change_candidates
                    if item.candidate_id == target.candidate_id
                ),
                None,
            )
            if candidate is not None:
                payload["candidate"] = candidate.model_dump(mode="json")
                exact_changes.append(
                    f"长期记忆：{candidate.memory_type}/{candidate.operation}"
                )
        elif target.target_kind == "habit_profile" and result.habit_change_set:
            payload["change_set"] = result.habit_change_set.model_dump(mode="json")
            exact_changes.extend(
                f"习惯画像：{item.concept_id}/{item.operation.value}"
                for item in result.habit_change_set.candidates
            )
        elif target.target_kind == "care":
            care = next(
                (
                    item
                    for item in result.accepted_work_products
                    if item.agent_id == ProductAgentId.CARE_STRATEGY
                ),
                None,
            )
            if care is not None:
                payload["care_strategy"] = care.payload
                exact_changes.append("建立、调整或结束主要照护行动")
        elif target.target_kind == "external_action":
            if result.external_action_target is not None:
                payload["external_target"] = (
                    result.external_action_target.model_dump(mode="json")
                )
                exact_changes.append(
                    f"对外操作：{result.external_action_target.tool_name}"
                )
        proposal = ActionProposal(
            proposal_id=f"proposal:{target.confirmation_id}",
            task_id=task.task_id,
            episode_id=request.episode_id,
            subject_id=target.subject_id,
            proposer_actor_id=target.actor_id,
            action_kind=target.target_kind,
            action_scope=target.action_scope,
            target_id=target.candidate_id,
            target_hash=target.candidate_hash,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            policy_version=HITL_POLICY_VERSION,
            payload=payload,
            explanation=DecisionExplanation(
                what_will_change=target.reason,
                why_now=(
                    "SleepAgent 已形成具体候选，但在持久化或产生外部影响前"
                    "必须由承担责任的人确认。"
                ),
                who_will_receive_or_be_affected=(
                    "老人本人"
                    if target.target_kind != "external_action"
                    else "老人本人以及该操作中列明的外部接收方"
                ),
                duration_or_frequency=(
                    "仅批准本次、当前版本的目标；持续行动可在记录与管理中撤回。"
                ),
                how_to_revoke=(
                    "执行前可在待办中撤回；执行后可在记录与管理中停止持续行动，"
                    "已完成的外部发送不能被系统远程收回。"
                ),
                exact_changes=exact_changes,
            ),
            created_at=datetime.now(timezone.utc),
            expires_at=target.expires_at,
            authorization_id=task.authorization_id,
            metadata={
                "doctor_material": request.doctor_material,
                "professional_review_required": (
                    request.doctor_material
                    and target.target_kind == "external_action"
                ),
            },
        )
        decision = self.human_decisions.create(proposal)
        self.service.emit_event(
            task.task_id,
            event_type="human_decision.requested",
            message=target.reason,
            payload={
                "decision_id": decision.decision_id,
                "risk_level": decision.risk_level.value,
                "route": decision.route.value,
                "target_hash": target.candidate_hash,
                "required_roles": [
                    item.role for item in decision.requirements
                ],
            },
        )
        return decision

    def resume_product_after_confirmations(
        self,
        task_id: str,
    ) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        if (
            task.runtime_kind != "product_episode"
            or task.status != RadarTaskStatus.RUNNING
        ):
            raise InvalidTaskTransition(
                "product confirmation resume requires a running task"
            )
        checkpoint = self._latest_product_checkpoint(task_id)
        request = ProductEpisodeRunRequest.model_validate(
            checkpoint.payload["request"]
        )
        if "result" not in checkpoint.payload:
            raise InvalidTaskTransition(
                "legacy checkpoint has no frozen Product Episode result"
            )
        frozen_result = ProductEpisodeRunResult.model_validate(
            checkpoint.payload["result"]
        )
        targets = [
            PendingConfirmationTarget.model_validate(item)
            for item in checkpoint.payload.get(
                "pending_confirmations",
                [],
            )
        ]
        decisions = self.human_decisions.repository.list(task_id=task_id)
        by_target = {
            (
                item.proposal.target_id,
                item.proposal.target_hash,
                item.proposal.action_scope,
            ): item
            for item in decisions
        }
        tokens: dict[str, ConfirmationToken] = {}
        declined_confirmation_ids: set[str] = set()
        executing_decision_ids: list[str] = []
        for target in targets:
            decision = by_target.get(
                (
                    target.candidate_id,
                    target.candidate_hash,
                    target.action_scope,
                )
            )
            if decision is None:
                raise InvalidTaskTransition(
                    "frozen target has no authoritative human decision"
                )
            decision = self.human_decisions.expire(decision.decision_id)
            if decision.status in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
            }:
                raise InvalidTaskTransition(
                    "product task still has pending human decisions"
                )
            if decision.status in {
                HumanDecisionStatus.REJECTED,
                HumanDecisionStatus.REVOKED,
                HumanDecisionStatus.EXPIRED,
                HumanDecisionStatus.SUPERSEDED,
            }:
                declined_confirmation_ids.add(target.confirmation_id)
                continue
            grant = self.human_decisions.approval_grant(
                decision.decision_id
            )
            token = ConfirmationToken(
                token_id=grant.grant_id,
                candidate_id=target.candidate_id,
                candidate_hash=target.candidate_hash,
                actor_id=grant.approver_actor_id,
                actor_role=grant.approver_role,
                subject_id=target.subject_id,
                action_scope=target.action_scope,
                expires_at=target.expires_at,
                decision_id=grant.decision_id,
                policy_version=grant.policy_version,
                fact_snapshot_hash=grant.fact_snapshot_hash,
                authorization_id=grant.authorization_id,
                role_binding_id=grant.role_binding_id,
                grant_hash=grant.grant_hash,
            )
            tokens[target.confirmation_id] = token
            executing_decision_ids.append(decision.decision_id)
        try:
            for decision_id in executing_decision_ids:
                self.human_decisions.mark_execution(
                    decision_id,
                    status=HumanDecisionStatus.EXECUTING,
                )
            result = self.product_runner.commit_frozen_confirmations(
                request=request,
                frozen_result=frozen_result,
                confirmations=tokens,
                declined_confirmation_ids=tuple(
                    sorted(declined_confirmation_ids)
                ),
            )
        except Exception as exc:
            for decision_id in executing_decision_ids:
                self.human_decisions.mark_execution(
                    decision_id,
                    status=HumanDecisionStatus.EXECUTION_FAILED,
                    failure_reason=type(exc).__name__,
                )
            raise
        return self._execute_product_task(
            task,
            request,
            precomputed_result=result,
        )

    def resume_product_after_user_input(
        self,
        task_id: str,
        *,
        request: UserInputRequest,
        answer: str,
    ) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        if (
            task.runtime_kind != "product_episode"
            or task.status != RadarTaskStatus.WAITING_FOR_USER_INPUT
        ):
            raise InvalidTaskTransition(
                "product user-input resume requires a waiting task"
            )
        checkpoint = self._latest_product_checkpoint(task_id)
        episode_request = ProductEpisodeRunRequest.model_validate(
            checkpoint.payload["request"]
        )
        responses = {
            item.request_id: item
            for item in episode_request.user_fact_responses
        }
        role = task.role
        if role == "system":
            raise InvalidTaskTransition(
                "system role cannot supply a personal user fact"
            )
        responses[request.question_id] = ProductUserFactResponse(
            request_id=request.question_id,
            answer=answer,
            actor_id=task.requested_by_user_id or "system",
            actor_role=role,
            subject_id=task.subject_id,
            observed_at=datetime.now(timezone.utc),
        )
        resumed = episode_request.model_copy(
            update={
                "user_fact_responses": tuple(
                    responses[key] for key in sorted(responses)
                )
            }
        )
        self.service.transition_task(
            task_id,
            RadarTaskStatus.RUNNING,
            message="Product Episode resumed with reviewed user input.",
        )
        return self._execute_product_task(
            self.service.get_task(task_id),
            resumed,
        )

    def _complete_product_confirmation_actions(
        self,
        *,
        task: RadarAgentTask,
        result: ProductEpisodeRunResult,
        targets: list[PendingConfirmationTarget],
    ) -> None:
        if not (
            result.committed_memory_candidate_ids
            or result.committed_habit_change_set_id
            or result.committed_care_candidate_id
            or result.external_action_receipt_id
        ):
            return
        committed_memory = set(result.committed_memory_candidate_ids)
        for target in targets:
            execution_ref: str | None = None
            delivery_status: str | None = None
            if (
                target.target_kind == "memory"
                and target.candidate_id in committed_memory
            ):
                execution_ref = (
                    f"{result.receipt.trace_ref}:memory:"
                    f"{target.candidate_id}"
                )
            elif (
                target.target_kind == "habit_profile"
                and target.candidate_id
                == result.committed_habit_change_set_id
            ):
                execution_ref = (
                    f"{result.receipt.trace_ref}:habit-profile:"
                    f"{target.candidate_id}"
                )
            elif (
                target.target_kind == "care"
                and target.candidate_id
                == result.committed_care_candidate_id
            ):
                execution_ref = (
                    f"{result.receipt.trace_ref}:care:"
                    f"{target.candidate_id}"
                )
            elif (
                target.target_kind == "external_action"
                and result.external_action_receipt_id
            ):
                execution_ref = result.external_action_receipt_id
                delivery_status = (
                    result.external_action_delivery_status or "pending"
                )
            if execution_ref is None:
                continue
            matching_decisions = [
                item
                for item in self.human_decisions.repository.list(
                    task_id=task.task_id
                )
                if item.proposal.target_id == target.candidate_id
                and item.proposal.target_hash == target.candidate_hash
                and item.proposal.action_scope == target.action_scope
            ]
            if matching_decisions:
                self.human_decisions.mark_execution(
                    matching_decisions[-1].decision_id,
                    status=HumanDecisionStatus.COMMITTED,
                    receipt_ref=execution_ref,
                )
            confirmation_id = _product_confirmation_id(
                task.task_id,
                target,
            )
            self.service.complete_confirmation_action(
                task.task_id,
                confirmation_id,
                actor_id="ProductEpisodeRunner",
                actor_role="system",
                execution_ref=execution_ref,
                delivery_status=delivery_status,
            )

    def _product_request_for_task(
        self,
        task: RadarAgentTask,
        *,
        user_text: str | None = None,
        force_dialogue: bool = False,
        audience_role: str | None = None,
    ) -> ProductEpisodeRunRequest:
        scenario = get_replay_scenario(task.scenario)
        source = scenario.deterministic_input
        goal = task.goal_payload or {}
        goal_type = ProductGoalType(
            goal.get("goal_type", ProductGoalType.NIGHT_REVIEW.value)
        )
        episode_type = (
            EpisodeType.GROUNDED_DIALOGUE
            if force_dialogue
            else _product_episode_type(goal_type)
        )
        as_of = source.now
        date_end = source.night_report.night_of
        if episode_type == EpisodeType.TREND_REVIEW:
            date_start = date_end - timedelta(days=6)
            scope_kind = SourceScopeKind.SEVEN_DAY
        else:
            date_start = date_end
            scope_kind = SourceScopeKind.CURRENT_NIGHT
        scope = SourceScope(
            kind=scope_kind,
            as_of=as_of,
            timezone_name=source.timezone_name,
            date_start=date_start,
            date_end=date_end,
            # Multi-metric compatibility display only.  Authoritative counts
            # live in FactSnapshot-bound MetricReadinessDecision items.
            valid_night_count=0,
        )
        trend_summaries = _replay_trend_summaries(
            scenario,
            subject_id=task.subject_id,
            radar_device_id=task.radar_device_id,
        )
        trend_source_refs = tuple(
            item.source_report_ref
            for item in trend_summaries
            if item.source_report_ref is not None
        )
        canonical_data = {
            "night_report": source.night_report.model_dump(mode="json"),
            "snapshots": [
                item.model_dump(mode="json") for item in source.snapshots
            ],
            "alerts": [
                item.model_dump(mode="json") for item in source.alerts
            ],
            "trend_summary": [
                item.model_dump(mode="json") for item in trend_summaries
            ],
            "device_status": source.device_status,
            "anomalies": source.anomalies,
        }
        source_refs = tuple(
            [
                (
                    f"night-report:{task.radar_device_id}:"
                    f"{source.night_report.night_of.isoformat()}"
                ),
                *(
                    f"snapshot:{item.snapshot_id}"
                    for item in source.snapshots
                ),
                *trend_source_refs,
            ]
        )
        claim_kind = (
            ClaimKind.LONGITUDINAL_TREND
            if goal_type == ProductGoalType.TREND_COMPARISON
            else ClaimKind.DESCRIBE_CURRENT_NIGHT
        )
        readiness_decisions = build_unavailable_entry_decisions(
            decision_namespace=f"{task.task_id}:cold-start",
            claim_kind=claim_kind,
        )
        snapshot = FactSnapshot.create(
            fact_snapshot_id=(
                f"fact:{task.task_id}:"
                f"{stable_hash((task.task_version, user_text or ''))[:16]}"
            ),
            binding=AuthenticatedBinding(
                actor_id=task.requested_by_user_id or "system",
                subject_id=task.subject_id,
                role=task.role if task.role != "system" else "family",
                authorization_scope=(
                    "read_sleep_data",
                    "read_device_data",
                    "draft_material",
                ),
            ),
            source_scope=scope,
            canonical_data_version=stable_hash(canonical_data),
            care_context_version=self.product_runner.commit_controller.care_store.get(
                task.subject_id
            ).version,
            memory_context_version=self.product_runner.commit_controller.memory_store.get(
                task.subject_id
            ).version,
            source_refs=source_refs,
            **snapshot_binding_material(decisions=readiness_decisions),
            created_at=datetime.now(timezone.utc),
        )
        question = (
            user_text
            if user_text is not None
            else str(
                goal.get("question")
                or "请基于当前授权信息完成睡眠复盘。"
            )
        )
        common_tool_input = {
            "data": canonical_data,
            "source_refs": list(source_refs),
        }
        tool_inputs = {
            "radar.get_night_evidence": common_tool_input,
            "radar.get_range_evidence": common_tool_input,
            "radar.assess_data_quality": {
                "coverage_ratio": (
                    source.night_report.data_coverage_ratio
                ),
                "missing_intervals": (
                    source.night_report.missing_intervals
                ),
                "source_refs": list(source_refs),
            },
            "radar.get_device_status": {
                "data": {"device_status": source.device_status},
                "source_refs": list(source_refs),
            },
            "trend.calculate_metrics": {
                "night_summaries": [
                    item.model_dump(mode="json")
                    for item in trend_summaries
                ],
                "source_refs": list(trend_source_refs),
            },
        }
        return ProductEpisodeRunRequest(
            episode_id=(
                f"task:{task.task_id}:"
                f"{'chat' if force_dialogue else 'run'}:"
                f"{stable_hash(question)[:12]}"
            ),
            episode_type=episode_type,
            objective=_product_objective(goal_type, question),
            fact_snapshot=snapshot,
            runtime_readiness_decisions=readiness_decisions,
            user_text=question,
            audience_role=(
                audience_role
                or (task.role if task.role != "system" else "family")
            ),
            tool_inputs=tool_inputs,
            personalized=True,
            doctor_material=goal_type == ProductGoalType.DOCTOR_MATERIAL,
            idempotency_key=task.idempotency_key or task.task_id,
        )

    def detail(self, task_id: str) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        all_artifacts = self.service.list_artifacts(task_id)
        artifacts = [
            item
            for item in all_artifacts
            if item.artifact_type
            != PRODUCT_EPISODE_CHECKPOINT_ARTIFACT
        ]
        ledger = _latest_ledger(artifacts)
        product_artifacts = [
            item
            for item in artifacts
            if item.artifact_type == "product_episode_result"
        ]
        try:
            receipt = self.store.get_completion_receipt(task_id)
        except KeyError:
            receipt = None
        if product_artifacts:
            receipt_payload = product_artifacts[-1].payload.get("receipt")
            if receipt_payload:
                receipt = EpisodeReceipt.model_validate(receipt_payload)
        elif receipt is not None and hasattr(receipt, "model_dump"):
            # Historical dynamic receipts remain readable without importing
            # their executable runtime contracts into the canonical API.
            receipt = receipt.model_dump(mode="json")
        risk_level = (
            str(ledger.derived_metrics.get("risk_level"))
            if ledger
            else (
                str(product_artifacts[-1].metadata.get("risk_level"))
                if product_artifacts
                else None
            )
        )
        return RadarTaskDetail(
            task=task,
            risk_level=risk_level,
            replay_scenario=(
                get_replay_scenario(task.scenario)
                if task.runtime_kind == "legacy_fixed"
                or os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() == "true"
                else None
            ),
            artifacts=artifacts,
            confirmations=self.service.list_confirmations(task_id),
            decisions=(
                self.human_decisions.repository.list(task_id=task_id)
                if task.runtime_kind == "product_episode"
                else []
            ),
            questionnaire_candidates=_workflow_questionnaire_candidates(artifacts),
            user_input_requests=(
                [
                    UserInputRequest.model_validate(
                        item.model_dump(mode="python")
                    )
                    for item in self.store.list_user_input_requests(task_id)
                ]
                if task.runtime_kind
                in {"dynamic_goal", "product_episode"}
                else []
            ),
            completion_receipt=receipt,
        )

    def answer_chat(self, payload: RadarChatRequest) -> RadarChatResponse:
        task = self.service.get_task(payload.task_id)
        _assert_task_runtime_writable(task)
        return self._answer_product_chat(task, payload)

    def _answer_product_chat(
        self,
        task: RadarAgentTask,
        payload: RadarChatRequest,
    ) -> RadarChatResponse:
        result = self.product_runner.run(
            self._product_request_for_task(
                task,
                user_text=payload.message,
                force_dialogue=True,
                audience_role=payload.role,
            )
        )
        publication = result.publication
        if publication is None:
            raise InvalidTaskTransition(
                "Product Episode did not produce a publishable answer"
            )
        self.service.save_payload_artifact(
            task.task_id,
            artifact_id=f"product-chat:{task.task_id}",
            artifact_type="product_episode_chat",
            payload={
                "receipt": result.receipt.model_dump(mode="json"),
                "publication": publication.model_dump(mode="json"),
            },
            source_refs=list(publication.claim_refs),
            metadata={
                "runtime_kind": "product_episode",
                "audience_role": payload.role,
            },
        )
        self.service.emit_event(
            task.task_id,
            event_type="chat.answered",
            message=(
                "Grounded chat answer generated by ProductEpisodeRunner."
            ),
            payload={
                "role": payload.role,
                "episode_id": result.receipt.episode_id,
                "agent_invocation_ids": (
                    result.receipt.agent_invocation_ids
                ),
                "evidence_refs": publication.claim_refs,
                "safety_decision_refs": (
                    result.receipt.safety_decision_refs
                ),
            },
        )
        knowledge_refs = [
            binding.source_ref
            for binding in publication.semantic_bindings
            if binding.source_kind == "general_knowledge"
        ]
        return RadarChatResponse(
            task_id=task.task_id,
            role=publication.audience_role,
            answer=publication.text,
            evidence_refs=publication.claim_refs,
            rag_citation_refs=knowledge_refs,
            boundary_action=(
                "blocked"
                if result.receipt.status == EpisodeStatus.BLOCKED
                else None
            ),
            generation_mode=result.receipt.execution_mode.value,
            facts_mutated=False,
        )


_RUNTIME = RadarApiRuntime()
router = APIRouter(prefix=RADAR_AGENT_API_PREFIX, tags=["radar-agent"])


def reset_radar_api_runtime_for_tests(
    connection: sqlite3.Connection | None = None,
    *,
    product_runner: ProductEpisodeRunner | None = None,
) -> RadarApiRuntime:
    global _RUNTIME
    _RUNTIME = RadarApiRuntime(
        connection or sqlite3.connect(":memory:", check_same_thread=False),
        product_runner=product_runner,
    )
    return _RUNTIME


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv(RADAR_AGENT_API_KEY_ENV) or os.getenv(PRODUCT_RADAR_API_KEY_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="Radar Agent API authentication is not configured.")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Radar Agent API authentication required.")


def _assert_actor(task: RadarAgentTask, actor_id: str, actor_role: str) -> None:
    if actor_role == "system":
        return
    if task.requested_by_user_id != actor_id:
        raise HTTPException(status_code=403, detail="Actor cannot access this task.")
    if actor_role != task.role:
        raise HTTPException(status_code=403, detail="Actor role cannot access this task.")


def _identity(
    task: RadarAgentTask,
    *,
    actor_id: str | None,
    actor_role: str | None,
) -> tuple[str, str]:
    if not actor_id or not actor_role:
        raise HTTPException(
            status_code=403,
            detail="x-actor-id and x-actor-role are required for task access.",
        )
    resolved_id = actor_id
    resolved_role = actor_role
    _assert_actor(task, resolved_id, resolved_role)
    if (
        task.runtime_kind == "product_episode"
        and not _development_mode_enabled()
    ):
        try:
            _RUNTIME.service.assert_task_access(
                task.task_id,
                actor_id=resolved_id,
                actor_role=resolved_role,
            )
        except (AuthorizationRequired, BindingMismatch, RoleAccessDenied) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return resolved_id, resolved_role


def _development_mode_enabled() -> bool:
    return os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() == "true"


def _assert_task_runtime_writable(task: RadarAgentTask) -> None:
    if task.runtime_kind != "product_episode":
        raise InvalidTaskTransition(
            f"historical {task.runtime_kind!r} task is read-only"
        )


@router.post("/tasks", response_model=RadarTaskDetail)
async def create_radar_task(
    payload: RadarTaskCreateRequest,
    x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
    x_subject_id: str | None = Header(default=None),
    x_authorization_id: str | None = Header(default=None),
    x_role_binding_ids: str | None = Header(default=None),
) -> RadarTaskDetail:
    _require_api_key(x_api_key)
    try:
        if not x_actor_id or x_actor_role not in {"elder", "family", "doctor"}:
            raise RoleAccessDenied(
                "Agent task creation requires authenticated actor and role headers"
            )
        payload = payload.model_copy(
            update={"actor_id": x_actor_id, "role": x_actor_role}
        )
        if not _development_mode_enabled():
            role_binding_ids = tuple(
                item.strip()
                for item in (x_role_binding_ids or "").split(",")
                if item.strip()
            )
            if (
                not x_subject_id
                or not x_authorization_id
                or not role_binding_ids
            ):
                raise AuthorizationRequired(
                    "product task requires server-bound subject, "
                    "authorization and role binding headers"
                )
            payload = payload.model_copy(
                update={
                    "subject_id": x_subject_id,
                    "authorization_id": x_authorization_id,
                    "role_binding_ids": list(role_binding_ids),
                }
            )
        task = _RUNTIME.create_task(payload, idempotency_key=idempotency_key)
        return _RUNTIME.detail(task.task_id)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RoleAccessDenied, AuthorizationRequired, BindingMismatch) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/run", response_model=None)
async def run_radar_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> RadarTaskDetail | Response:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        return _RUNTIME.run_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc
    except InvalidTaskTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/user-input")
async def answer_radar_task_user_input(
    task_id: str,
    payload: RadarUserInputAnswerRequest,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Response:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _assert_task_runtime_writable(task)
        actor_id, actor_role = _identity(
            task, actor_id=x_actor_id, actor_role=x_actor_role
        )
        if task.status != RadarTaskStatus.WAITING_FOR_USER_INPUT:
            raise InvalidTaskTransition("task is not waiting for user input")
        request = _RUNTIME.store.get_user_input_request(payload.request_id)
        if request.task_id != task_id:
            raise ValueError("user input request is not active for this task")
        if request.target_role != actor_role:
            raise ValueError("answering role does not match the reviewed request")
        if payload.declined:
            _RUNTIME.store.decline_user_input_request(request.request_id)
            _RUNTIME.service.emit_event(
                task_id,
                event_type="user_input.decline_submitted",
                message="User declined the reviewed input request.",
                payload={
                    "request_id": request.request_id,
                    "question_id": request.question_id,
                },
            )
            current = _RUNTIME.service.get_task(task_id).model_copy(
                update={
                    "completion_status": "partial",
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            _RUNTIME.store.save_task(current)
            _RUNTIME.service.transition_task(
                task_id,
                RadarTaskStatus.FAILED,
                message=(
                    "Product Episode stopped because the user declined "
                    "the required fact."
                ),
            )
        else:
            response = UserInputResponse(
                response_id=f"response:{request.request_id}",
                request_id=request.request_id,
                task_id=task_id,
                answer=payload.answer or "",
                answered_by_user_id=actor_id,
                answered_by_role=actor_role,
            )
            _RUNTIME.store.save_user_input_response(response)
            _RUNTIME.service.emit_event(
                task_id,
                event_type="user_input.submitted",
                message="User supplied self-reported context for the active task.",
                payload={
                    "request_id": request.request_id,
                    "question_id": request.question_id,
                },
            )
            _RUNTIME.resume_product_after_user_input(
                task_id,
                request=request,
                answer=payload.answer or "",
            )
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task_id,
                "request_id": request.request_id,
                "status": "declined" if payload.declined else "accepted",
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task or user input request not found.") from exc
    except (InvalidTaskTransition, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks", response_model=list[RadarTaskDetail])
async def list_radar_tasks(
    view: Literal["active", "history"] = Query(default="active"),
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> list[RadarTaskDetail]:
    _require_api_key(x_api_key)
    active = {
        RadarTaskStatus.CREATED,
        RadarTaskStatus.RUNNING,
        RadarTaskStatus.WAITING_FOR_USER_INPUT,
        RadarTaskStatus.WAITING_FOR_CONFIRMATION,
    }
    tasks = _RUNTIME.store.list_tasks()
    tasks = [task for task in tasks if (task.status in active) == (view == "active")]
    visible: list[RadarTaskDetail] = []
    for task in tasks:
        try:
            _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        except HTTPException:
            continue
        visible.append(_RUNTIME.detail(task.task_id))
    return visible


@router.get("/tasks/{task_id}", response_model=RadarTaskDetail)
async def get_radar_task(
    task_id: str,
    artifact_id: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> RadarTaskDetail:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        detail = _RUNTIME.detail(task_id)
        if artifact_id is not None:
            matches = _RUNTIME.service.list_artifacts(task_id, artifact_id=artifact_id)
            if not matches:
                raise HTTPException(status_code=404, detail="Radar task artifact not found.")
            detail = detail.model_copy(
                update={"artifacts": matches}
            )
        return detail
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc


@router.get("/tasks/{task_id}/events", response_model=list[RadarTaskEvent])
async def get_radar_task_events(
    task_id: str,
    after_sequence: int = Query(default=0, ge=0),
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> list[RadarTaskEvent]:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        return _RUNTIME.service.list_events(task_id, after_sequence=after_sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc


@router.get("/tasks/{task_id}/decision-trace")
async def get_radar_task_decision_trace(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        visible_prefixes = (
            "goal.",
            "plan.",
            "agent.",
            "tool.",
            "a2a.",
            "user_input.",
            "confirmation.",
            "human_decision.",
            "execution.",
            "task.",
        )
        events = [
            event
            for event in _RUNTIME.service.list_events(task_id)
            if event.event_type.startswith(visible_prefixes)
        ]
        return {
            "task_id": task_id,
            "execution_mode": task.execution_mode,
            "completion_status": task.completion_status,
            "entries": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "summary": event.message,
                    "created_at": event.created_at,
                    "details": {
                        key: value
                        for key, value in event.payload.items()
                        if key
                        in {
                            "goal_type",
                            "execution_mode",
                            "completion_status",
                            "revision",
                            "capabilities",
                            "agent",
                            "capability",
                            "confidence",
                            "uncertainty_count",
                            "tool_name",
                            "sender",
                            "receiver",
                            "intent",
                            "decision",
                            "decision_id",
                            "risk_level",
                            "route",
                            "required_roles",
                            "target_hash",
                            "decision_summary",
                            "question_type",
                            "why_needed",
                            "reason_code",
                            "checkpoint_kind",
                            "changed_fields",
                            "source_agent_invocation_id",
                            "target_agent_invocation_id",
                            "causal_source_agent_invocation_id",
                            "resolution_status",
                            "request_type",
                            "skip_reason",
                            "reused_from_plan_id",
                            "action_type",
                            "artifact_version_id",
                        }
                    },
                }
                for event in events
            ],
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc


@router.get("/tasks/{task_id}/developer-trace")
async def get_radar_task_developer_trace(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_api_key(x_api_key)
    if os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Developer trace is disabled.")
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        plans = _RUNTIME.store.list_execution_plans(task_id) if task.runtime_kind == "dynamic_goal" else []
        return {
            "task": {
                "task_id": task.task_id,
                "trace_id": task.trace_id,
                "runtime_kind": task.runtime_kind,
                "runtime_contract_version": task.runtime_contract_version,
                "execution_mode": task.execution_mode,
                "completion_status": task.completion_status,
                "status": task.status.value,
            },
            "plans": [plan.model_dump(mode="json") for plan in plans],
            "model_invocations": [
                item.model_dump(mode="json")
                for item in _RUNTIME.store.list_model_invocations(task_id)
            ],
            "agent_invocations": [
                item.model_dump(mode="json")
                for item in _RUNTIME.store.list_agent_invocations(task_id)
            ],
            "tool_invocations": [
                item.model_dump(mode="json")
                for item in _RUNTIME.store.list_tool_invocations(task_id)
            ],
            "a2a": [
                item.model_dump(mode="json")
                for item in _RUNTIME.service.list_a2a_messages(task_id)
            ],
            "budget": (
                _RUNTIME.store.get_runtime_budget(task_id).model_dump(mode="json")
                if task.runtime_kind == "dynamic_goal"
                else None
            ),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc


@router.get("/tasks/{task_id}/stream")
async def stream_radar_task_events(
    task_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Response:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc
    cursor = _sse_cursor(request.headers.get("last-event-id"), after_sequence)

    if task.status in {
        RadarTaskStatus.COMPLETED,
        RadarTaskStatus.FAILED,
        RadarTaskStatus.CANCELLED,
        RadarTaskStatus.WAITING_FOR_USER_INPUT,
        RadarTaskStatus.WAITING_FOR_CONFIRMATION,
    }:
        return Response(
            content=_sse_history(task_id, after_sequence=cursor),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    async def live_events():
        live_cursor = cursor
        while True:
            batch = _RUNTIME.service.list_events(
                task_id,
                after_sequence=live_cursor,
            )
            for event in batch:
                live_cursor = event.sequence
                yield _format_sse_event(event)
            current = _RUNTIME.service.get_task(task_id)
            if current.status in {
                RadarTaskStatus.COMPLETED,
                RadarTaskStatus.FAILED,
                RadarTaskStatus.CANCELLED,
                RadarTaskStatus.WAITING_FOR_USER_INPUT,
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
            }:
                break
            if await request.is_disconnected():
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        live_events(),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@router.post("/tasks/{task_id}/confirm", response_model=HumanConfirmationRequest)
async def confirm_radar_task(
    task_id: str,
    payload: RadarConfirmationResolveRequest,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
    x_role_binding_id: str | None = Header(default=None),
    x_role_binding_ids: str | None = Header(default=None),
    x_authorization_id: str | None = Header(default=None),
) -> HumanConfirmationRequest:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _assert_task_runtime_writable(task)
        if task.runtime_kind == "product_episode":
            if not x_actor_id or x_actor_role not in {
                "elder",
                "family",
                "doctor",
            }:
                raise RoleAccessDenied(
                    "human decisions require authenticated actor and role headers"
                )
            actor_id, actor_role = x_actor_id, x_actor_role
            projection = _RUNTIME.store.get_confirmation(
                payload.confirmation_id
            )
            if len(projection.evidence_refs) < 2:
                raise InvalidTaskTransition(
                    "product confirmation lacks an exact target binding"
                )
            target_id, target_hash = projection.evidence_refs[:2]
            matching = [
                item
                for item in _RUNTIME.human_decisions.repository.list(
                    task_id=task_id
                )
                if item.proposal.target_id == target_id
                and item.proposal.target_hash == target_hash
            ]
            if len(matching) != 1:
                raise InvalidTaskTransition(
                    "product confirmation has no unique authoritative decision"
                )
            decision = _RUNTIME.human_decisions.decide(
                matching[0].decision_id,
                actor_id=actor_id,
                actor_role=actor_role,
                choice=(
                    HumanDecisionChoice.APPROVE
                    if payload.approved
                    else HumanDecisionChoice.REJECT
                ),
                target_hash=target_hash,
                reason=payload.reason,
                role_binding_id=(
                    x_role_binding_id
                    or next(
                        (
                            item.strip()
                            for item in (x_role_binding_ids or "").split(",")
                            if item.strip()
                        ),
                        None,
                    )
                ),
                authorization_id=x_authorization_id,
            )
            if decision.status in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
            }:
                return projection
        resolved = _RUNTIME.service.resolve_confirmation(
            task_id,
            payload.confirmation_id,
            approved=decision.status == HumanDecisionStatus.APPROVED,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        if task.runtime_kind == "product_episode":
            pending = [
                item
                for item in _RUNTIME.service.list_confirmations(task_id)
                if item.status == "pending" and item.blocks_daily_flow
            ]
            if not pending:
                _RUNTIME.resume_product_after_confirmations(task_id)
                resolved = _RUNTIME.store.get_confirmation(
                    resolved.confirmation_id
                )
        return resolved
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task or confirmation not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/human-decisions/{decision_id}/revoke",
    response_model=HumanDecisionRequest,
)
async def revoke_product_human_decision(
    decision_id: str,
    payload: RadarDecisionRevokeRequest,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
    x_role_binding_id: str | None = Header(default=None),
    x_role_binding_ids: str | None = Header(default=None),
    x_authorization_id: str | None = Header(default=None),
) -> HumanDecisionRequest:
    _require_api_key(x_api_key)
    try:
        if not x_actor_id or x_actor_role not in {
            "elder",
            "family",
            "doctor",
        }:
            raise RoleAccessDenied(
                "decision revocation requires authenticated actor and role headers"
            )
        decision = _RUNTIME.human_decisions.repository.get(decision_id)
        task_id = decision.proposal.task_id
        if task_id is None:
            raise InvalidTaskTransition(
                "control-plane decisions use the release governance endpoint"
            )
        task = _RUNTIME.service.get_task(task_id)
        _assert_task_runtime_writable(task)
        role_binding_id = x_role_binding_id or next(
            (
                item.strip()
                for item in (x_role_binding_ids or "").split(",")
                if item.strip()
            ),
            None,
        )
        revoked = _RUNTIME.human_decisions.revoke(
            decision_id,
            actor_id=x_actor_id,
            actor_role=x_actor_role,
            role_binding_id=role_binding_id,
            authorization_id=x_authorization_id,
        )
        projections = [
            item
            for item in _RUNTIME.service.list_confirmations(task_id)
            if item.evidence_refs[:2]
            == [
                decision.proposal.target_id,
                decision.proposal.target_hash,
            ]
        ]
        if projections and projections[-1].execution_status != "completed":
            _RUNTIME.service.revoke_confirmation(
                task_id,
                projections[-1].confirmation_id,
                actor_id=x_actor_id,
                actor_role=x_actor_role,
                reason=payload.reason,
            )
        if task.status == RadarTaskStatus.RUNNING:
            _RUNTIME.resume_product_after_confirmations(task_id)
        return revoked
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Decision not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/chat", response_model=RadarChatResponse)
async def radar_task_chat(
    payload: RadarChatRequest,
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> RadarChatResponse:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(payload.task_id)
        actor_id, actor_role = _identity(
            task,
            actor_id=x_actor_id,
            actor_role=x_actor_role,
        )
        return _RUNTIME.answer_chat(
            payload.model_copy(
                update={
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                }
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc
    except InvalidTaskTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _latest_ledger(artifacts: list[RadarArtifactVersion]) -> EvidenceLedger | None:
    candidates = [item for item in artifacts if item.evidence_ledger is not None]
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: (item.version, item.created_at))
    return latest.evidence_ledger


def _goal_output(goal_type: ProductGoalType | None) -> str:
    mapping = {
        ProductGoalType.NIGHT_REVIEW: "summary",
        ProductGoalType.TREND_COMPARISON: "trend",
        ProductGoalType.CHANGE_EXPLANATION: "explanation",
        ProductGoalType.DATA_QUALITY_DIAGNOSIS: "quality",
        ProductGoalType.DOCTOR_MATERIAL: "doctor_material",
        ProductGoalType.GROUNDED_QUESTION: "answer",
    }
    if goal_type is None:
        raise ValueError("product goal type is required")
    return mapping[goal_type]


def _product_episode_type(goal_type: ProductGoalType) -> EpisodeType:
    return {
        ProductGoalType.NIGHT_REVIEW: EpisodeType.MORNING_REVIEW,
        ProductGoalType.TREND_COMPARISON: EpisodeType.TREND_REVIEW,
        ProductGoalType.CHANGE_EXPLANATION: EpisodeType.GROUNDED_DIALOGUE,
        ProductGoalType.DATA_QUALITY_DIAGNOSIS: (
            EpisodeType.DATA_QUALITY_RECOVERY
        ),
        ProductGoalType.DOCTOR_MATERIAL: EpisodeType.ROLE_MATERIAL,
        ProductGoalType.GROUNDED_QUESTION: EpisodeType.GROUNDED_DIALOGUE,
    }[goal_type]


def _product_objective(goal_type: ProductGoalType, question: str) -> str:
    labels = {
        ProductGoalType.NIGHT_REVIEW: "完成当前夜睡眠复盘",
        ProductGoalType.TREND_COMPARISON: "解释已授权时间范围内的睡眠趋势",
        ProductGoalType.CHANGE_EXPLANATION: "回答变化原因问题并保留不确定性",
        ProductGoalType.DATA_QUALITY_DIAGNOSIS: "解释数据质量并给出恢复提示",
        ProductGoalType.DOCTOR_MATERIAL: "生成受证据和安全审查约束的医生材料",
        ProductGoalType.GROUNDED_QUESTION: "回答基于当前授权证据的问题",
    }
    return f"{labels[goal_type]}：{question}"


def _product_completion_status(status: EpisodeStatus) -> str:
    if status == EpisodeStatus.COMPLETE:
        return "complete"
    if status in {
        EpisodeStatus.PARTIAL,
        EpisodeStatus.WAITING_USER,
        EpisodeStatus.WAITING_CONFIRMATION,
    }:
        return "partial"
    return "blocked"


def _product_confirmation_id(
    task_id: str,
    target: PendingConfirmationTarget,
) -> str:
    return (
        f"product-confirmation:{task_id}:"
        f"{stable_hash(target.model_dump(mode='json'))[:20]}"
    )


def _product_user_input_id(task_id: str, request_id: str) -> str:
    return (
        f"product-user-input:{task_id}:"
        f"{stable_hash(request_id)[:20]}"
    )


def _workflow_questionnaire_candidates(
    artifacts: list[RadarArtifactVersion],
) -> list[QuestionnaireCandidate]:
    snapshots = [item for item in artifacts if item.artifact_type == "workflow_run"]
    if not snapshots:
        return []
    latest = max(snapshots, key=lambda item: (item.version, item.created_at))
    return [
        QuestionnaireCandidate.model_validate(item)
        for item in latest.payload.get("questionnaire_candidates", [])
    ]


def _replay_trend_summaries(
    scenario: Any,
    *,
    subject_id: str,
    radar_device_id: str,
) -> list[RadarNightSummary]:
    night_of = scenario.deterministic_input.night_report.night_of
    summaries: list[RadarNightSummary] = []
    for point in scenario.deterministic_input.trend_summary:
        month, day = (int(value) for value in point.date.split("/", maxsplit=1))
        point_date = date(night_of.year, month, day)
        summaries.append(
            RadarNightSummary(
                radar_device_id=radar_device_id,
                subject_id=subject_id,
                night_of=point_date,
                timezone_name=scenario.deterministic_input.timezone_name,
                total_sleep_minutes=point.total_sleep_minutes,
                sleep_score=point.sleep_score,
                out_of_bed_count=point.getup_count,
                data_coverage_ratio=1.0,
                explainable_metrics={
                    "available_metrics": ["sleep_minutes", "out_of_bed_count"]
                },
                # The scenario selects a development fixture only. Persist an
                # opaque canonical source ref so fixture labels cannot reach
                # planner, Agent, artifact, or trace inputs.
                source_report_ref=(
                    f"history-night:{radar_device_id}:{point_date.isoformat()}"
                ),
            )
        )
    return summaries


def _sse_cursor(last_event_id: str | None, after_sequence: int) -> int:
    if not last_event_id:
        return after_sequence
    try:
        return max(after_sequence, int(last_event_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer sequence.") from exc


def _sse_history(task_id: str, *, after_sequence: int) -> str:
    chunks = [
        _format_sse_event(event)
        for event in _RUNTIME.service.list_events(
            task_id,
            after_sequence=after_sequence,
        )
    ]
    return "".join([*chunks, ": keep-alive\n\n"])


def _format_sse_event(event: RadarTaskEvent) -> str:
    payload = event.model_dump(mode="json")
    return (
        f"id: {event.sequence}\n"
        f"event: {event.event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


__all__ = [
    "RADAR_AGENT_API_KEY_ENV",
    "RADAR_AGENT_DATABASE_URL_ENV",
    "RADAR_AGENT_LLM_RETRY_ENV",
    "RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV",
    "RADAR_AGENT_DEV_MODE_ENV",
    "RADAR_AGENT_SQLITE_PATH_ENV",
    "RadarApiRuntime",
    "RadarChatRequest",
    "RadarChatResponse",
    "RadarTaskCreateRequest",
    "RadarTaskDetail",
    "reset_radar_api_runtime_for_tests",
    "router",
]
