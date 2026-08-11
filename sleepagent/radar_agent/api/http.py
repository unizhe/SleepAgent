from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum
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
)
from sleepagent.radar_agent.persistence.history import (
    HistoricalRecord,
    HistoricalTaskRecord,
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
    build_developer_trace,
)
from sleepagent.radar_agent.schemas import (
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentSchema,
    RadarNightSummary,
)
from sleepagent.radar_agent.product_agent.cold_start import (
    ClaimKind,
    build_unavailable_entry_decisions,
    snapshot_binding_material,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId as ProductAgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    ApprovalRequirement,
    DecisionExplanation,
    DecisionRoute,
    HumanDecisionChoice,
    HumanDecisionRecord,
    HumanDecisionRequest,
    HumanDecisionStatus,
    HumanRiskLevel,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
    CommitFrozenConfirmedAction,
    PendingConfirmationTarget,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductUserFactResponse,
    ReexecuteWithAddedFact,
    bind_product_episode_checkpoint,
    product_episode_frozen_identity_hash,
    product_episode_request_hash,
)
from sleepagent.radar_agent.product_agent.runtime_factory import (
    ProductRuntimeBundle,
    build_product_runtime_bundle_from_env,
    product_episode_runner_is_configured,
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
PRODUCT_EPISODE_CHECKPOINT_ARTIFACT = "_product_episode_checkpoint"
PRODUCT_EPISODE_REQUEST_ARTIFACT = "_product_episode_request"
_PRODUCT_INTERNAL_ARTIFACT_TYPES = {
    PRODUCT_EPISODE_CHECKPOINT_ARTIFACT,
    PRODUCT_EPISODE_REQUEST_ARTIFACT,
}


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


class HumanDecisionPublicView(RadarAgentSchema):
    """Wire-safe HDS view that deliberately excludes the persisted grant."""

    decision_id: str
    proposal: ActionProposal
    risk_level: HumanRiskLevel
    route: DecisionRoute
    requirements: list[ApprovalRequirement] = Field(default_factory=list)
    status: HumanDecisionStatus
    decisions: list[HumanDecisionRecord] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    execution_receipt_ref: str | None = None
    failure_reason: str | None = None
    superseded_by: str | None = None
    revision: int = Field(..., ge=0)

    @classmethod
    def from_authority(
        cls,
        decision: HumanDecisionRequest,
    ) -> "HumanDecisionPublicView":
        return cls.model_validate(
            decision.model_dump(mode="python", exclude={"active_grant"})
        )


class RadarTaskDetail(RadarAgentSchema):
    task: RadarAgentTask | HistoricalTaskRecord
    risk_level: str | None = None
    replay_scenario: ReplayScenario | None = None
    artifacts: list[RadarArtifactVersion] = Field(default_factory=list)
    confirmations: list[HumanConfirmationRequest] = Field(default_factory=list)
    decisions: list[HumanDecisionPublicView] = Field(default_factory=list)
    questionnaire_candidates: list[dict[str, Any]] = Field(
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
        *,
        product_runtime: ProductRuntimeBundle,
    ) -> None:
        if product_runtime.persistence_store is None:
            raise ValueError(
                "Radar API requires a persistent Product runtime bundle"
            )
        self.store = product_runtime.persistence_store
        # API-key authentication and per-task ownership are enforced at the HTTP
        # boundary. Domain deployments can pre-provision authorization records and
        # replace this runtime without changing the routes.
        self.service = TaskService(
            self.store,
            validate_bindings=False,
            require_authorization=False,
        )
        self._lock = RLock()
        self.product_runtime = product_runtime
        self.human_decisions = self.product_runtime.human_decisions
        self.product_runner = self.product_runtime.runner

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
            task = self.service.transition_task(
                task.task_id,
                RadarTaskStatus.RUNNING,
                message="Product Episode accepted for four-role execution.",
            )
        elif task.status != RadarTaskStatus.RUNNING:
            raise InvalidTaskTransition(
                f"product task in {task.status.value!r} state cannot be run"
            )
        recovered = self._recover_terminal_product_projection(
            task,
            allow_nonterminal_checkpoint=True,
        )
        if recovered is not None:
            return recovered
        episode_request = self._product_request_for_task(task)
        return self._execute_product_task(task, episode_request)

    def _recover_terminal_product_projection(
        self,
        task: RadarAgentTask,
        *,
        allow_nonterminal_checkpoint: bool = False,
    ) -> RadarTaskDetail | None:
        if task.status != RadarTaskStatus.RUNNING:
            return None

        # The Product result store is the durable authority for completed
        # Runner work.  An API process can fail after finalization/publication
        # but before its task checkpoint is written; in that window a retry
        # must project the stored result rather than invoke or publish again.
        episode_id = self._product_request_for_task(task).episode_id
        try:
            durable_result = self.product_runtime.stores.episode_results.latest(
                episode_id
            )
        except KeyError:
            durable_result = None

        try:
            checkpoint = self._latest_product_checkpoint(task.task_id)
        except InvalidTaskTransition:
            checkpoint = None

        checkpoint_result: ProductEpisodeRunResult | None = None
        if checkpoint is not None:
            raw_result = checkpoint.payload.get("result")
            if raw_result is not None:
                checkpoint_result = ProductEpisodeRunResult.model_validate(
                    raw_result
                )

        durable_is_newer = durable_result is not None and (
            checkpoint_result is None
            or durable_result.receipt.receipt_revision
            > checkpoint_result.receipt.receipt_revision
            or (
                durable_result.receipt.receipt_revision
                == checkpoint_result.receipt.receipt_revision
                and durable_result.receipt.terminal
                and not checkpoint_result.receipt.terminal
            )
        )
        if durable_is_newer:
            assert durable_result is not None
            request = self._product_request_intent_for_result(
                task=task,
                result=durable_result,
            )
            durable_result = self._validated_durable_product_result(
                request,
                durable_result,
            )
            return self._execute_product_task(
                task,
                request,
                precomputed_result=durable_result,
                emit_precomputed_agent_events=True,
            )

        if checkpoint is None or checkpoint_result is None:
            return None
        result = checkpoint_result
        if not result.receipt.terminal:
            if not allow_nonterminal_checkpoint:
                return None
            _, result = self._validated_continuation_checkpoint(checkpoint)
            if not self._product_waiting_authority_is_active(
                task=task,
                result=result,
            ):
                raise InvalidTaskTransition(
                    "Product continuation must be retried through its "
                    "reviewed interaction endpoint"
                )
            return self._project_product_task_result(
                task,
                result,
                targets=result.pending_confirmations,
            )
        request, result = self._validated_terminal_checkpoint(checkpoint)
        previous_targets: list[PendingConfirmationTarget] = []
        if (
            result.continuation_kind
            == "commit_frozen_confirmed_action"
            or result.committed_memory_candidate_ids
            or result.committed_habit_change_set_id is not None
            or result.committed_care_candidate_id is not None
            or result.external_action_receipt_id is not None
            or result.declined_confirmation_ids
        ):
            waiting_checkpoint = self._latest_product_checkpoint(
                task.task_id,
                pending_kind="confirmation",
            )
            waiting_request, waiting_result = (
                self._validated_continuation_checkpoint(
                    waiting_checkpoint
                )
            )
            if waiting_request.episode_id != request.episode_id:
                raise InvalidTaskTransition(
                    "terminal confirmation checkpoint Episode mismatch"
                )
            if (
                result.continuation_kind
                == "commit_frozen_confirmed_action"
                and result.continuation_parent_checkpoint_hash
                != product_episode_frozen_identity_hash(waiting_result)
            ):
                raise InvalidTaskTransition(
                    "terminal confirmation checkpoint lineage mismatch"
                )
            previous_targets = waiting_result.pending_confirmations
        return self._project_product_task_result(
            task,
            result,
            targets=previous_targets,
        )

    def _product_waiting_authority_is_active(
        self,
        *,
        task: RadarAgentTask,
        result: ProductEpisodeRunResult,
    ) -> bool:
        pending_user_input = result.pending_user_input
        if pending_user_input is not None:
            request_id = _product_user_input_id(
                task.task_id,
                pending_user_input.request_id,
            )
            try:
                request = self.store.get_user_input_request(request_id)
            except KeyError:
                return False
            return bool(request.status == "pending")
        if result.pending_confirmations:
            decisions: list[HumanDecisionRequest] = []
            for target in result.pending_confirmations:
                if target.decision_id is None:
                    return False
                try:
                    decisions.append(
                        self.human_decisions.get(target.decision_id)
                    )
                except KeyError:
                    return False
            return any(
                decision.status
                in {
                    HumanDecisionStatus.PENDING,
                    HumanDecisionStatus.PARTIALLY_APPROVED,
                }
                for decision in decisions
            )
        return False

    def _execute_product_task(
        self,
        task: RadarAgentTask,
        episode_request: ProductEpisodeRunRequest,
        *,
        precomputed_result: ProductEpisodeRunResult | None = None,
        emit_precomputed_agent_events: bool = False,
    ) -> RadarTaskDetail:
        episode_request = self._record_product_request_intent(
            task,
            episode_request,
        )
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
        existing_events = self.service.list_events(task.task_id)
        emitted_invocations = {
            (
                str(event.payload.get("invocation_id")),
                str(event.payload.get("context_hash")),
                str(event.payload.get("target_hash")),
            )
            for event in existing_events
            if getattr(event, "event_type", None) == "agent.completed"
            and isinstance(getattr(event, "payload", None), dict)
            and event.payload.get("invocation_id") is not None
            and event.payload.get("context_hash") is not None
            and event.payload.get("target_hash") is not None
        }
        for invocation in (
            result.agent_invocations
            if precomputed_result is None or emit_precomputed_agent_events
            else []
        ):
            invocation_identity = (
                invocation.invocation_id,
                invocation.context_hash,
                invocation.target_hash,
            )
            if invocation_identity in emitted_invocations:
                continue
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
                    "context_hash": invocation.context_hash,
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
        emitted_tool_invocation_ids = {
            str(event.payload.get("tool_invocation_id"))
            for event in existing_events
            if getattr(event, "event_type", None) == "tool.completed"
            and isinstance(getattr(event, "payload", None), dict)
            and event.payload.get("tool_invocation_id") is not None
        }
        for receipt in [
            item
            for item in result.tool_receipts
            if item.tool_invocation_id not in previous_receipt_ids
            and item.tool_invocation_id not in emitted_tool_invocation_ids
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
        result = self._register_product_interactions(
            task=task,
            request=episode_request,
            result=result,
        )
        if result.receipt.status in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            # HDS decision/proposal IDs are attached by the API after Runner
            # finalization. Bind only after that final trusted mutation.
            result = bind_product_episode_checkpoint(result)
        result_payload = {
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
            "external_action_target_hash": result.external_action_target_hash,
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
        }
        self._save_product_artifact_once(
            task=task,
            artifact_id=f"product-episode:{task.task_id}",
            artifact_type="product_episode_result",
            payload=result_payload,
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
        return self._project_product_task_result(
            task,
            result,
            targets=previous_targets or result.pending_confirmations,
        )

    def _project_product_task_result(
        self,
        task: RadarAgentTask,
        result: ProductEpisodeRunResult,
        *,
        targets: list[PendingConfirmationTarget],
    ) -> RadarTaskDetail:
        """Project an already-durable Product result into task/HDS views."""

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
        # Confirmation projections are retryable while the task remains
        # RUNNING.  The externally visible terminal transition is last.
        self._complete_product_confirmation_actions(
            task=task,
            result=result,
            targets=targets,
        )
        self.service.transition_task(
            task.task_id,
            status,
            message=(
                "Product Episode finished with "
                f"{result.receipt.status.value}."
            ),
        )
        return self.detail(task.task_id)

    def _save_product_checkpoint(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
    ) -> None:
        if any(
            target.decision_id is None or target.proposal_id is None
            for target in result.pending_confirmations
        ):
            raise InvalidTaskTransition(
                "Product checkpoint requires explicit HDS decision bindings"
            )
        self._save_product_artifact_once(
            task=task,
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

    def _record_product_request_intent(
        self,
        task: RadarAgentTask,
        candidate: ProductEpisodeRunRequest,
    ) -> ProductEpisodeRunRequest:
        """Durably freeze the exact request before any Runner side effect."""

        intent_key = self._product_request_intent_key(candidate)
        matches: list[ProductEpisodeRunRequest] = []
        for artifact in self.service.list_artifacts(task.task_id):
            if artifact.artifact_type != PRODUCT_EPISODE_REQUEST_ARTIFACT:
                continue
            request = self._validated_product_request_artifact(task, artifact)
            if artifact.payload.get("intent_key") == intent_key:
                matches.append(request)
        if matches:
            identities = {
                self._product_request_retry_identity(item) for item in matches
            }
            candidate_identity = self._product_request_retry_identity(candidate)
            if identities != {candidate_identity}:
                raise InvalidTaskTransition(
                    "persisted Product request intent conflicts with retry"
                )
            return matches[0]

        request_hash = product_episode_request_hash(candidate)
        self._save_product_artifact_once(
            task=task,
            artifact_id=f"product-request:{task.task_id}",
            artifact_type=PRODUCT_EPISODE_REQUEST_ARTIFACT,
            payload={
                "request": candidate.model_dump(mode="json"),
                "request_hash": request_hash,
                "intent_key": intent_key,
            },
            source_refs=[candidate.fact_snapshot.fact_snapshot_id],
            metadata={
                "runtime_kind": "product_episode",
                "internal": True,
            },
        )
        return candidate

    def _product_request_intent_for_result(
        self,
        *,
        task: RadarAgentTask,
        result: ProductEpisodeRunResult,
    ) -> ProductEpisodeRunRequest:
        expected_hash = result.continuation_request_hash
        matches: list[ProductEpisodeRunRequest] = []
        for artifact in self.service.list_artifacts(task.task_id):
            if artifact.artifact_type != PRODUCT_EPISODE_REQUEST_ARTIFACT:
                continue
            request = self._validated_product_request_artifact(task, artifact)
            if (
                expected_hash is not None
                and product_episode_request_hash(request) == expected_hash
            ):
                matches.append(request)
        if not matches:
            raise InvalidTaskTransition(
                "durable Product result has no exact persisted request intent"
            )
        hashes = {product_episode_request_hash(item) for item in matches}
        if len(hashes) != 1:
            raise InvalidTaskTransition(
                "durable Product result has ambiguous request intent"
            )
        return matches[0]

    @staticmethod
    def _product_request_intent_key(
        request: ProductEpisodeRunRequest,
    ) -> str:
        return stable_hash(
            {
                "episode_id": request.episode_id,
                "continuation_lineage": (
                    request.continuation_lineage.model_dump(mode="json")
                    if request.continuation_lineage is not None
                    else None
                ),
            }
        )

    @staticmethod
    def _product_request_retry_identity(
        request: ProductEpisodeRunRequest,
    ) -> str:
        payload = request.model_dump(mode="json")
        snapshot = payload["fact_snapshot"]
        for field in (
            "fact_snapshot_hash",
            "created_at",
            "care_context_version",
            "memory_context_version",
        ):
            snapshot.pop(field, None)
        return stable_hash(payload)

    def _validated_product_request_artifact(
        self,
        task: RadarAgentTask,
        artifact: RadarArtifactVersion,
    ) -> ProductEpisodeRunRequest:
        if "request" not in artifact.payload:
            raise InvalidTaskTransition(
                "persisted Product request intent has no request"
            )
        request = ProductEpisodeRunRequest.model_validate(
            artifact.payload["request"]
        )
        request_hash = product_episode_request_hash(request)
        expected_fact_snapshot_ids = {
            f"fact:{task.task_id}:"
            f"{stable_hash((task.task_version, value))[:16]}"
            for value in ("", request.user_text or "")
        }
        if (
            artifact.payload.get("request_hash") != request_hash
            or artifact.payload.get("intent_key")
            != self._product_request_intent_key(request)
            or request.fact_snapshot.binding.subject_id != task.subject_id
            or request.fact_snapshot.binding.actor_id
            != (task.requested_by_user_id or "system")
            or request.fact_snapshot.fact_snapshot_id
            not in expected_fact_snapshot_ids
            or not request.episode_id.startswith(f"task:{task.task_id}:")
        ):
            raise InvalidTaskTransition(
                "persisted Product request intent binding mismatch"
            )
        return request

    @staticmethod
    def _validated_durable_product_result(
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
    ) -> ProductEpisodeRunResult:
        if (
            result.receipt.episode_id != request.episode_id
            or result.receipt.fact_snapshot_id
            != request.fact_snapshot.fact_snapshot_id
            or result.receipt.fact_snapshot_hash
            != request.fact_snapshot.fact_snapshot_hash
        ):
            raise InvalidTaskTransition(
                "durable Product result binding mismatch"
            )
        request_hash = product_episode_request_hash(request)
        if result.continuation_request_hash is None:
            if result.schema_version != "ProductEpisodeRunResult.v38":
                raise InvalidTaskTransition(
                    "current durable Product result lacks its request hash"
                )
            return result.model_copy(
                update={"continuation_request_hash": request_hash}
            )
        if result.continuation_request_hash != request_hash:
            raise InvalidTaskTransition(
                "durable Product result request hash mismatch"
            )
        return result

    def _save_product_artifact_once(
        self,
        *,
        task: RadarAgentTask,
        artifact_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        source_refs: list[str],
        metadata: dict[str, Any],
    ) -> RadarArtifactVersion:
        for existing in self.service.list_artifacts(
            task.task_id,
            artifact_id=artifact_id,
        ):
            if (
                existing.artifact_type == artifact_type
                and existing.payload == payload
                and existing.source_refs == source_refs
                and existing.metadata == metadata
            ):
                return existing
        return self.service.save_payload_artifact(
            task.task_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            payload=payload,
            source_refs=source_refs,
            metadata=metadata,
        )

    def _emit_product_event_once(
        self,
        *,
        task_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        identity: dict[str, Any],
    ) -> None:
        for event in self.service.list_events(task_id):
            event_payload = getattr(event, "payload", None)
            if (
                getattr(event, "event_type", None) == event_type
                and isinstance(event_payload, dict)
                and all(
                    event_payload.get(key) == value
                    for key, value in identity.items()
                )
            ):
                return
        self.service.emit_event(
            task_id,
            event_type=event_type,
            message=message,
            payload=payload,
        )

    def _latest_product_checkpoint(
        self,
        task_id: str,
        *,
        pending_kind: Literal["confirmation", "user_input"] | None = None,
    ) -> RadarArtifactVersion:
        candidates = [
            item
            for item in self.service.list_artifacts(task_id)
            if item.artifact_type
            == PRODUCT_EPISODE_CHECKPOINT_ARTIFACT
        ]
        if pending_kind == "confirmation":
            candidates = [
                item
                for item in candidates
                if item.payload.get("pending_confirmations")
            ]
        elif pending_kind == "user_input":
            candidates = [
                item
                for item in candidates
                if item.payload.get("pending_user_input") is not None
            ]
        if not candidates:
            raise InvalidTaskTransition(
                "product task has no resumable checkpoint"
            )
        return max(candidates, key=lambda item: (item.version, item.created_at))

    @staticmethod
    def _validated_terminal_checkpoint(
        checkpoint: RadarArtifactVersion,
    ) -> tuple[ProductEpisodeRunRequest, ProductEpisodeRunResult]:
        if "result" not in checkpoint.payload:
            raise InvalidTaskTransition(
                "persisted checkpoint has no Product Episode result"
            )
        request = ProductEpisodeRunRequest.model_validate(
            checkpoint.payload["request"]
        )
        result = ProductEpisodeRunResult.model_validate(
            checkpoint.payload["result"]
        )
        if not result.receipt.terminal:
            raise InvalidTaskTransition("checkpoint result is not terminal")
        request_hash = product_episode_request_hash(request)
        if (
            result.receipt.episode_id != request.episode_id
            or result.receipt.fact_snapshot_id
            != request.fact_snapshot.fact_snapshot_id
            or result.receipt.fact_snapshot_hash
            != request.fact_snapshot.fact_snapshot_hash
        ):
            raise InvalidTaskTransition(
                "terminal Product checkpoint binding mismatch"
            )
        if result.continuation_request_hash is None:
            if result.schema_version != "ProductEpisodeRunResult.v38":
                raise InvalidTaskTransition(
                    "current terminal checkpoint lacks its request hash"
                )
            result = result.model_copy(
                update={"continuation_request_hash": request_hash}
            )
        elif result.continuation_request_hash != request_hash:
            raise InvalidTaskTransition(
                "terminal Product checkpoint request hash mismatch"
            )
        return request, result

    @staticmethod
    def _validated_continuation_checkpoint(
        checkpoint: RadarArtifactVersion,
    ) -> tuple[ProductEpisodeRunRequest, ProductEpisodeRunResult]:
        if "result" not in checkpoint.payload:
            raise InvalidTaskTransition(
                "persisted checkpoint has no frozen Product Episode result"
            )
        request = ProductEpisodeRunRequest.model_validate(
            checkpoint.payload["request"]
        )
        frozen = ProductEpisodeRunResult.model_validate(
            checkpoint.payload["result"]
        )
        if (
            frozen.receipt.episode_id != request.episode_id
            or frozen.receipt.fact_snapshot_id
            != request.fact_snapshot.fact_snapshot_id
            or frozen.receipt.fact_snapshot_hash
            != request.fact_snapshot.fact_snapshot_hash
        ):
            raise InvalidTaskTransition(
                "persisted continuation checkpoint binding mismatch"
            )
        expected_request_hash = product_episode_request_hash(request)
        if frozen.continuation_request_hash is None:
            if frozen.schema_version != "ProductEpisodeRunResult.v38":
                raise InvalidTaskTransition(
                    "current continuation checkpoint lacks its request hash"
                )
            frozen = frozen.model_copy(
                update={"continuation_request_hash": expected_request_hash}
            )
        elif frozen.continuation_request_hash != expected_request_hash:
            raise InvalidTaskTransition(
                "persisted continuation request hash mismatch"
            )
        if frozen.continuation_checkpoint_hash is None:
            if frozen.schema_version not in {
                "ProductEpisodeRunResult.v38",
                "ProductEpisodeRunResult.v39",
            }:
                raise InvalidTaskTransition(
                    "current continuation checkpoint lacks its result hash"
                )
            frozen = bind_product_episode_checkpoint(frozen)
        elif frozen.schema_version == PRODUCT_EPISODE_RESULT_SCHEMA_VERSION:
            # Current checkpoints arrive pre-bound; model validation above is
            # the integrity check. Never rebind a current loaded checkpoint.
            pass
        return request, frozen

    def _register_product_interactions(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
    ) -> ProductEpisodeRunResult:
        bound_targets: list[PendingConfirmationTarget] = []
        for target in result.pending_confirmations:
            decision = self._ensure_product_human_decision(
                task=task,
                request=request,
                result=result,
                target=target,
            )
            if target.decision_id not in {None, decision.decision_id} or (
                target.proposal_id
                not in {None, decision.proposal.proposal_id}
            ):
                raise InvalidTaskTransition(
                    "Product confirmation carries a conflicting HDS binding"
                )
            bound_target = target.model_copy(
                update={
                    "decision_id": decision.decision_id,
                    "proposal_id": decision.proposal.proposal_id,
                }
            )
            bound_targets.append(bound_target)
            self.store.save_hds_confirmation_projection(
                self._project_human_decision(
                    task_id=task.task_id,
                    target=bound_target,
                    decision=decision,
                )
            )
        bound_result = result.model_copy(
            update={"pending_confirmations": bound_targets}
        )
        pending = result.pending_user_input
        if pending is None:
            return bound_result
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
            self._emit_product_event_once(
                task_id=task.task_id,
                event_type="user_input.requested",
                message=pending.question_text,
                payload={
                    "request_id": request_id,
                    "source_agent": pending.source_agent.value,
                    "decision_scope": pending.decision_scope,
                },
                identity={"request_id": request_id},
            )
            return bound_result
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
        self._emit_product_event_once(
            task_id=task.task_id,
            event_type="user_input.requested",
            message=pending.question_text,
            payload={
                "request_id": request_id,
                "source_agent": pending.source_agent.value,
                "decision_scope": pending.decision_scope,
            },
            identity={"request_id": request_id},
        )
        return bound_result

    def _ensure_product_human_decision(
        self,
        *,
        task: RadarAgentTask,
        request: ProductEpisodeRunRequest,
        result: ProductEpisodeRunResult,
        target: PendingConfirmationTarget,
    ) -> HumanDecisionRequest:
        if target.decision_id is not None:
            decision = self._bound_human_decision(
                task_id=task.task_id,
                target=target,
                request=request,
            )
            self._emit_product_human_decision_requested(
                task=task,
                target=target,
                decision=decision,
            )
            return decision
        expected_proposal_id = f"proposal:{target.confirmation_id}"
        existing = [
            item
            for item in self.human_decisions.list(task_id=task.task_id)
            if item.proposal.proposal_id == expected_proposal_id
            and item.proposal.episode_id == request.episode_id
            and item.proposal.subject_id == target.subject_id
            and item.proposal.target_hash == target.candidate_hash
            and item.proposal.target_id == target.candidate_id
            and item.proposal.action_scope == target.action_scope
            and item.proposal.fact_snapshot_id
            == request.fact_snapshot.fact_snapshot_id
            and item.proposal.fact_snapshot_hash
            == request.fact_snapshot.fact_snapshot_hash
            and item.proposal.policy_version == HITL_POLICY_VERSION
            and item.proposal.expires_at == target.expires_at
        ]
        if existing:
            if len(existing) != 1:
                raise InvalidTaskTransition(
                    "Product confirmation has ambiguous HDS authority"
                )
            decision = existing[0]
            self._emit_product_human_decision_requested(
                task=task,
                target=target,
                decision=decision,
            )
            return decision
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
            proposal_id=expected_proposal_id,
            task_id=task.task_id,
            episode_id=request.episode_id,
            subject_id=target.subject_id,
            proposer_actor_id=target.actor_id,
            action_kind=target.target_kind,
            action_scope=target.action_scope,
            target_id=target.candidate_id,
            target_hash=target.candidate_hash,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
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
        self._emit_product_human_decision_requested(
            task=task,
            target=target,
            decision=decision,
        )
        return decision

    def _emit_product_human_decision_requested(
        self,
        *,
        task: RadarAgentTask,
        target: PendingConfirmationTarget,
        decision: HumanDecisionRequest,
    ) -> None:
        self._emit_product_event_once(
            task_id=task.task_id,
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
            identity={"decision_id": decision.decision_id},
        )

    def _bound_human_decision(
        self,
        *,
        task_id: str,
        target: PendingConfirmationTarget,
        request: ProductEpisodeRunRequest | None = None,
    ) -> HumanDecisionRequest:
        if target.decision_id is None or target.proposal_id is None:
            raise InvalidTaskTransition(
                "Product confirmation is missing its explicit HDS binding"
            )
        try:
            decision = self.human_decisions.get(target.decision_id)
        except KeyError as exc:
            raise InvalidTaskTransition(
                "Product confirmation references an unknown HDS decision"
            ) from exc
        proposal = decision.proposal
        if (
            proposal.proposal_id != target.proposal_id
            or proposal.task_id != task_id
            or proposal.subject_id != target.subject_id
            or proposal.proposer_actor_id != target.actor_id
            or proposal.target_id != target.candidate_id
            or proposal.target_hash != target.candidate_hash
            or proposal.action_scope != target.action_scope
            or proposal.expires_at != target.expires_at
        ):
            raise InvalidTaskTransition(
                "Product confirmation HDS binding does not match its target"
            )
        if request is not None and (
            proposal.episode_id != request.episode_id
            or proposal.fact_snapshot_id
            != request.fact_snapshot.fact_snapshot_id
            or proposal.fact_snapshot_hash
            != request.fact_snapshot.fact_snapshot_hash
        ):
            raise InvalidTaskTransition(
                "Product confirmation HDS binding does not match its Episode"
            )
        return decision

    @staticmethod
    def _project_human_decision(
        *,
        task_id: str,
        target: PendingConfirmationTarget,
        decision: HumanDecisionRequest,
    ) -> HumanConfirmationRequest:
        """Build the legacy task view from the sole HDS authority record."""

        status = {
            HumanDecisionStatus.PENDING: "pending",
            HumanDecisionStatus.PARTIALLY_APPROVED: "pending",
            HumanDecisionStatus.APPROVED: "approved",
            HumanDecisionStatus.EXECUTING: "approved",
            HumanDecisionStatus.COMMITTED: "approved",
            HumanDecisionStatus.EXECUTION_FAILED: "approved",
            HumanDecisionStatus.OUTCOME_UNKNOWN: "approved",
            HumanDecisionStatus.REJECTED: "rejected",
            HumanDecisionStatus.HARD_BLOCKED: "rejected",
            HumanDecisionStatus.EXPIRED: "expired",
            HumanDecisionStatus.REVOKED: "revoked",
            HumanDecisionStatus.SUPERSEDED: "revoked",
        }[decision.status]
        resolved_by = (
            decision.decisions[-1].actor_id
            if decision.decisions
            else "human-decision-service"
        )
        execution_status = (
            "completed"
            if decision.status == HumanDecisionStatus.COMMITTED
            else (
                "failed"
                if decision.status
                in {
                    HumanDecisionStatus.EXECUTION_FAILED,
                    HumanDecisionStatus.OUTCOME_UNKNOWN,
                }
                else "not_started"
            )
        )
        confirmation_id = _product_confirmation_id(task_id, target)
        return HumanConfirmationRequest(
            confirmation_id=confirmation_id,
            decision_id=decision.decision_id,
            decision_revision=decision.revision,
            task_id=task_id,
            action_type=f"product_{target.target_kind}:{target.action_scope}",
            requested_role="elder",
            allowed_roles=[
                item.role
                for item in decision.requirements
                if item.role in {"elder", "family", "doctor", "system"}
            ],
            reason=target.reason,
            evidence_refs=[target.candidate_id, target.candidate_hash],
            status=status,
            idempotency_key=confirmation_id,
            created_at=decision.created_at,
            resolved_at=(decision.resolved_at if status != "pending" else None),
            resolved_by=(resolved_by if status in {"approved", "rejected", "expired"} else None),
            revoked_at=(decision.resolved_at if status == "revoked" else None),
            revoked_by=(resolved_by if status == "revoked" else None),
            execution_status=execution_status,
            execution_ref=decision.execution_receipt_ref,
            executed_at=(
                decision.updated_at
                if execution_status in {"completed", "failed"}
                else None
            ),
        )

    def resume_product_after_confirmations(
        self,
        task_id: str,
    ) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        if (
            not isinstance(task, RadarAgentTask)
            or task.status
            not in {
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
                RadarTaskStatus.RUNNING,
            }
        ):
            raise InvalidTaskTransition(
                "product confirmation resume requires a waiting task"
            )
        if task.status == RadarTaskStatus.RUNNING:
            recovered = self._recover_terminal_product_projection(task)
            if recovered is not None:
                return recovered
        checkpoint = self._latest_product_checkpoint(task_id)
        request, frozen_result = self._validated_continuation_checkpoint(
            checkpoint
        )
        targets = [
            PendingConfirmationTarget.model_validate(item)
            for item in checkpoint.payload.get(
                "pending_confirmations",
                [],
            )
        ]
        if targets != frozen_result.pending_confirmations:
            raise InvalidTaskTransition(
                "checkpoint confirmation bindings disagree with the frozen result"
            )
        for target in targets:
            decision = self._bound_human_decision(
                task_id=task_id,
                target=target,
                request=request,
            )
            decision = self.human_decisions.expire(decision.decision_id)
            if decision.status in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
            }:
                raise InvalidTaskTransition(
                    "product task still has pending human decisions"
                )
            if decision.status not in {
                HumanDecisionStatus.APPROVED,
                HumanDecisionStatus.EXECUTING,
                HumanDecisionStatus.COMMITTED,
                HumanDecisionStatus.EXECUTION_FAILED,
                HumanDecisionStatus.OUTCOME_UNKNOWN,
                HumanDecisionStatus.REJECTED,
                HumanDecisionStatus.REVOKED,
                HumanDecisionStatus.EXPIRED,
                HumanDecisionStatus.SUPERSEDED,
                HumanDecisionStatus.HARD_BLOCKED,
            }:
                raise InvalidTaskTransition(
                    "product task has a non-terminal human decision"
                )
        if task.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION:
            self.service.transition_task(
                task_id,
                RadarTaskStatus.RUNNING,
                message="Authoritative human decisions are ready for commit.",
            )
            task = self.service.get_task(task_id)
            assert isinstance(task, RadarAgentTask)
        request = self._record_product_request_intent(task, request)
        result = self.product_runner.commit_frozen_confirmations(
            CommitFrozenConfirmedAction(
                request=request,
                frozen_result=frozen_result,
            )
        )
        return self._execute_product_task(
            task,
            request,
            precomputed_result=result,
        )

    def resume_product_after_user_input(
        self,
        task_id: str,
        *,
        request_id: str,
    ) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        if (
            not isinstance(task, RadarAgentTask)
            or task.status
            not in {
                RadarTaskStatus.WAITING_FOR_USER_INPUT,
                RadarTaskStatus.RUNNING,
            }
        ):
            raise InvalidTaskTransition(
                "product user-input resume requires a waiting or resumable task"
            )
        persisted_request = self.store.get_user_input_request(request_id)
        persisted_response = self.store.get_user_input_response(request_id)
        if (
            persisted_request.task_id != task_id
            or persisted_request.status != "answered"
            or persisted_response is None
            or persisted_response.task_id != task_id
            or persisted_response.request_id != request_id
        ):
            raise InvalidTaskTransition(
                "product user-input resume requires an authoritative answer"
            )
        if task.status == RadarTaskStatus.RUNNING:
            recovered = self._recover_terminal_product_projection(task)
            if recovered is not None:
                return recovered
        checkpoint = self._latest_product_checkpoint(task_id)
        episode_request, frozen_result = (
            self._validated_continuation_checkpoint(checkpoint)
        )
        pending = frozen_result.pending_user_input
        if (
            pending is None
            or persisted_request.question_id != pending.request_id
            or _product_user_input_id(task_id, pending.request_id)
            != persisted_request.request_id
        ):
            raise InvalidTaskTransition(
                "answered request does not match the frozen user-input target"
            )
        role = task.role
        if role == "system":
            raise InvalidTaskTransition(
                "system role cannot supply a personal user fact"
            )
        added_fact = ProductUserFactResponse(
            request_id=persisted_request.question_id,
            answer=persisted_response.answer,
            actor_id=persisted_response.answered_by_user_id,
            actor_role=persisted_response.answered_by_role,
            subject_id=task.subject_id,
            observed_at=persisted_response.created_at,
        )
        if (
            added_fact.actor_id != (task.requested_by_user_id or "system")
            or added_fact.actor_role != role
        ):
            raise InvalidTaskTransition(
                "persisted user input does not match the task actor binding"
            )
        command = ReexecuteWithAddedFact(
            request=episode_request,
            frozen_result=frozen_result,
            added_fact=added_fact,
        )
        resumed = command.reexecution_request()
        if task.status == RadarTaskStatus.WAITING_FOR_USER_INPUT:
            self.service.transition_task(
                task_id,
                RadarTaskStatus.RUNNING,
                message="Product Episode resumed with reviewed user input.",
            )
        running_task = self.service.get_task(task_id)
        assert isinstance(running_task, RadarAgentTask)
        resumed = self._record_product_request_intent(
            running_task,
            resumed,
        )
        result = self.product_runner.reexecute_with_added_fact(command)
        return self._execute_product_task(
            running_task,
            resumed,
            precomputed_result=result,
            emit_precomputed_agent_events=True,
        )

    def _complete_product_confirmation_actions(
        self,
        *,
        task: RadarAgentTask,
        result: ProductEpisodeRunResult,
        targets: list[PendingConfirmationTarget],
    ) -> None:
        for target in targets:
            decision = self._bound_human_decision(
                task_id=task.task_id,
                target=target,
            )
            projection = self._project_human_decision(
                task_id=task.task_id,
                target=target,
                decision=decision,
            )
            if (
                target.target_kind == "external_action"
                and decision.status == HumanDecisionStatus.COMMITTED
            ):
                delivery_status = result.external_action_delivery_status or "pending"
                projection = projection.model_copy(
                    update={
                        "delivery_status": delivery_status,
                        "delivered_at": (
                            decision.updated_at
                            if delivery_status == "delivered"
                            else None
                        ),
                    }
                )
            self.store.save_hds_confirmation_projection(projection)

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
            care_context_version=self.product_runtime.stores.care_context.get(
                task.subject_id
            ).version,
            memory_context_version=self.product_runtime.stores.memory_context.get(
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
        resolved_audience_role = (
            audience_role
            or (task.role if task.role != "system" else "family")
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
        base_idempotency_key = task.idempotency_key or task.task_id
        chat_command_hash = stable_hash(
            {
                "question": question,
                "audience_role": resolved_audience_role,
            }
        )[:16]
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
            audience_role=resolved_audience_role,
            tool_inputs=tool_inputs,
            personalized=True,
            doctor_material=goal_type == ProductGoalType.DOCTOR_MATERIAL,
            idempotency_key=(
                f"{base_idempotency_key}:chat:{chat_command_hash}"
                if force_dialogue
                else base_idempotency_key
            ),
        )

    def detail(self, task_id: str) -> RadarTaskDetail:
        task = self.service.get_task(task_id)
        if isinstance(task, HistoricalTaskRecord):
            receipt = self.store.history.read_completion_receipt(task_id)
            return RadarTaskDetail(
                task=task,
                completion_receipt=(
                    receipt.to_dict() if receipt is not None else None
                ),
            )

        all_artifacts = self.service.list_artifacts(task_id)
        artifacts = [
            item
            for item in all_artifacts
            if item.artifact_type not in _PRODUCT_INTERNAL_ARTIFACT_TYPES
        ]
        ledger = _latest_ledger(artifacts)
        product_artifacts = [
            item
            for item in artifacts
            if item.artifact_type == "product_episode_result"
        ]
        receipt: dict[str, Any] | EpisodeReceipt | None = None
        if product_artifacts:
            receipt_payload = product_artifacts[-1].payload.get("receipt")
            if receipt_payload:
                receipt = EpisodeReceipt.model_validate(receipt_payload)
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
                if os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() == "true"
                else None
            ),
            artifacts=artifacts,
            confirmations=self.service.list_confirmations(task_id),
            decisions=[
                HumanDecisionPublicView.from_authority(item)
                for item in self.human_decisions.list(task_id=task_id)
            ],
            questionnaire_candidates=_workflow_questionnaire_candidates(artifacts),
            user_input_requests=[
                UserInputRequest.model_validate(item.model_dump(mode="python"))
                for item in self.store.list_user_input_requests(task_id)
            ],
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


_RUNTIME = RadarApiRuntime(
    product_runtime=build_product_runtime_bundle_from_env()
)
router = APIRouter(prefix=RADAR_AGENT_API_PREFIX, tags=["radar-agent"])


def get_radar_api_runtime() -> RadarApiRuntime:
    return _RUNTIME


def reset_radar_api_runtime_for_tests(
    connection: sqlite3.Connection | None = None,
    *,
    product_runtime: ProductRuntimeBundle | None = None,
) -> RadarApiRuntime:
    global _RUNTIME
    if product_runtime is not None and connection is not None:
        raise ValueError(
            "an injected Product runtime owns the API persistence store"
        )
    resolved_runtime = product_runtime
    if resolved_runtime is None:
        persistence = RadarPersistenceStore.connect_sqlite(
            connection
            or sqlite3.connect(":memory:", check_same_thread=False)
        )
        resolved_runtime = build_product_runtime_bundle_from_env(
            persistence_store=persistence,
        )
    _RUNTIME = RadarApiRuntime(product_runtime=resolved_runtime)
    from sleepagent.radar_agent.product_agent.habit_api import (
        configure_habit_profile_runtime,
    )

    configure_habit_profile_runtime(_RUNTIME.product_runtime)
    return _RUNTIME


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv(RADAR_AGENT_API_KEY_ENV) or os.getenv(PRODUCT_RADAR_API_KEY_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="Radar Agent API authentication is not configured.")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Radar Agent API authentication required.")


def _assert_actor(
    task: RadarAgentTask | HistoricalTaskRecord,
    actor_id: str,
    actor_role: str,
) -> None:
    if actor_role == "system":
        return
    if task.requested_by_user_id != actor_id:
        raise HTTPException(status_code=403, detail="Actor cannot access this task.")
    if actor_role != task.role:
        raise HTTPException(status_code=403, detail="Actor role cannot access this task.")


def _identity(
    task: RadarAgentTask | HistoricalTaskRecord,
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
    if isinstance(task, RadarAgentTask) and not _development_mode_enabled():
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


def _assert_task_runtime_writable(
    task: RadarAgentTask | HistoricalTaskRecord,
) -> None:
    if not isinstance(task, RadarAgentTask):
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
        request = _RUNTIME.store.get_user_input_request(payload.request_id)
        if request.task_id != task_id:
            raise ValueError("user input request is not active for this task")
        if request.target_role != actor_role:
            raise ValueError("answering role does not match the reviewed request")
        if task.status not in {
            RadarTaskStatus.WAITING_FOR_USER_INPUT,
            RadarTaskStatus.RUNNING,
        }:
            exact_decline = (
                payload.declined
                and task.status == RadarTaskStatus.FAILED
                and request.status == "declined"
                and any(
                    getattr(event, "event_type", None)
                    == "user_input.decline_submitted"
                    and isinstance(getattr(event, "payload", None), dict)
                    and event.payload.get("request_id") == request.request_id
                    for event in _RUNTIME.service.list_events(task_id)
                )
            )
            exact_answer = False
            if (
                not payload.declined
                and task.status
                in {RadarTaskStatus.COMPLETED, RadarTaskStatus.FAILED}
                and request.status == "answered"
            ):
                response = _RUNTIME.store.get_user_input_response(
                    request.request_id
                )
                exact_answer = bool(
                    response is not None
                    and response.task_id == task_id
                    and response.answer == (payload.answer or "")
                    and response.answered_by_user_id == actor_id
                    and response.answered_by_role == actor_role
                )
            if exact_decline or exact_answer:
                return JSONResponse(
                    status_code=202,
                    content={
                        "task_id": task_id,
                        "request_id": request.request_id,
                        "status": (
                            "declined" if payload.declined else "accepted"
                        ),
                    },
                )
            raise InvalidTaskTransition(
                "task is not waiting for or exactly retrying user input"
            )
        if payload.declined:
            if (
                task.status != RadarTaskStatus.WAITING_FOR_USER_INPUT
                or request.status not in {"pending", "declined"}
            ):
                raise InvalidTaskTransition(
                    "only a pending or exactly declined user-input request "
                    "can be projected"
                )
            _, newly_declined = _RUNTIME.store.decline_user_input_request(
                request.request_id
            )
            if newly_declined:
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
            if request.status == "pending":
                if task.status != RadarTaskStatus.WAITING_FOR_USER_INPUT:
                    pending_checkpoint = _RUNTIME._latest_product_checkpoint(
                        task_id,
                        pending_kind="user_input",
                    )
                    _, pending_result = (
                        _RUNTIME._validated_continuation_checkpoint(
                            pending_checkpoint
                        )
                    )
                    pending_target = pending_result.pending_user_input
                    if (
                        pending_target is None
                        or _product_user_input_id(
                            task_id,
                            pending_target.request_id,
                        )
                        != request.request_id
                    ):
                        raise InvalidTaskTransition(
                            "a running task has no matching pending input"
                        )
                proposed = UserInputResponse(
                    response_id=f"response:{request.request_id}",
                    request_id=request.request_id,
                    task_id=task_id,
                    answer=payload.answer or "",
                    answered_by_user_id=actor_id,
                    answered_by_role=actor_role,
                )
                response = _RUNTIME.store.save_user_input_response(proposed)
                if response == proposed:
                    _RUNTIME.service.emit_event(
                        task_id,
                        event_type="user_input.submitted",
                        message=(
                            "User supplied self-reported context for the "
                            "active task."
                        ),
                        payload={
                            "request_id": request.request_id,
                            "question_id": request.question_id,
                        },
                    )
            elif request.status == "answered":
                response = _RUNTIME.store.get_user_input_response(
                    request.request_id
                )
                if (
                    response is None
                    or response.task_id != task_id
                    or response.answer != (payload.answer or "")
                    or response.answered_by_user_id != actor_id
                    or response.answered_by_role != actor_role
                ):
                    raise InvalidTaskTransition(
                        "user-input retry does not match the persisted answer"
                    )
            else:
                raise InvalidTaskTransition(
                    "user input request is no longer answerable"
                )
            _RUNTIME.resume_product_after_user_input(
                task_id,
                request_id=request.request_id,
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
    active_values = {status.value for status in active}
    tasks = [
        task
        for task in tasks
        if (_task_status_value(task) in active_values) == (view == "active")
    ]
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
            matches = [
                item
                for item in _RUNTIME.service.list_artifacts(
                    task_id,
                    artifact_id=artifact_id,
                )
                if item.artifact_type not in _PRODUCT_INTERNAL_ARTIFACT_TYPES
            ]
            if not matches:
                raise HTTPException(status_code=404, detail="Radar task artifact not found.")
            detail = detail.model_copy(
                update={"artifacts": matches}
            )
        return detail
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar task not found.") from exc


@router.get("/tasks/{task_id}/events", response_model=None)
async def get_radar_task_events(
    task_id: str,
    after_sequence: int = Query(default=0, ge=0),
    x_api_key: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> list[RadarTaskEvent | dict[str, Any]]:
    _require_api_key(x_api_key)
    try:
        task = _RUNTIME.service.get_task(task_id)
        _identity(task, actor_id=x_actor_id, actor_role=x_actor_role)
        events = _RUNTIME.service.list_events(
            task_id,
            after_sequence=after_sequence,
        )
        return [
            event.to_dict() if isinstance(event, HistoricalRecord) else event
            for event in events
        ]
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
            "user_input.",
            "confirmation.",
            "human_decision.",
            "execution.",
            "task.",
        )
        events = [
            event
            for event in _RUNTIME.service.list_events(task_id)
            if _event_type(event).startswith(visible_prefixes)
        ]
        return {
            "task_id": task_id,
            "execution_mode": task.execution_mode,
            "completion_status": task.completion_status,
            "entries": [
                _decision_trace_entry(event)
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
        return build_developer_trace(_RUNTIME.service, task_id)
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

    if isinstance(task, HistoricalTaskRecord) or task.status in {
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
            if isinstance(current, HistoricalTaskRecord) or current.status in {
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
        if not x_actor_id or x_actor_role not in {
            "elder",
            "family",
            "doctor",
        }:
            raise RoleAccessDenied(
                "human decisions require authenticated actor and role headers"
            )
        actor_id, actor_role = x_actor_id, x_actor_role
        projection = _RUNTIME.store.get_confirmation(payload.confirmation_id)
        if projection.task_id != task_id:
            raise InvalidTaskTransition(
                "product confirmation belongs to another task"
            )
        checkpoint = _RUNTIME._latest_product_checkpoint(
            task_id,
            pending_kind="confirmation",
        )
        targets = [
            PendingConfirmationTarget.model_validate(item)
            for item in checkpoint.payload.get("pending_confirmations", [])
        ]
        matching_targets = [
            item
            for item in targets
            if _product_confirmation_id(task_id, item)
            == payload.confirmation_id
        ]
        if len(matching_targets) != 1:
            raise InvalidTaskTransition(
                "product confirmation has no unique frozen target"
            )
        target = matching_targets[0]
        authority = _RUNTIME._bound_human_decision(
            task_id=task_id,
            target=target,
        )
        if authority.decision_id != projection.decision_id:
            raise InvalidTaskTransition(
                "task projection does not match its authoritative decision"
            )
        choice = (
            HumanDecisionChoice.APPROVE
            if payload.approved
            else HumanDecisionChoice.REJECT
        )
        role_binding_id = x_role_binding_id or next(
            (
                item.strip()
                for item in (x_role_binding_ids or "").split(",")
                if item.strip()
            ),
            None,
        )
        prior_records = [
            item
            for item in authority.decisions
            if item.actor_id == actor_id
        ]
        exact_retry = bool(prior_records)
        if prior_records:
            if len(prior_records) != 1:
                raise InvalidTaskTransition(
                    "authoritative decision has duplicate actor records"
                )
            prior = prior_records[0]
            if (
                prior.actor_role != actor_role
                or prior.choice != choice
                or prior.target_hash != authority.proposal.target_hash
                or prior.reason != payload.reason
                or prior.role_binding_id != role_binding_id
                or prior.authorization_id != x_authorization_id
            ):
                raise InvalidTaskTransition(
                    "confirmation retry does not match the persisted decision"
                )
            decision = authority
        else:
            if task.status not in {
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
                RadarTaskStatus.RUNNING,
            }:
                raise InvalidTaskTransition(
                    "a terminal task cannot accept a new confirmation"
                )
            decision = _RUNTIME.human_decisions.decide(
                authority.decision_id,
                actor_id=actor_id,
                actor_role=actor_role,
                choice=choice,
                target_hash=authority.proposal.target_hash,
                reason=payload.reason,
                role_binding_id=role_binding_id,
                authorization_id=x_authorization_id,
            )
        resolved = _RUNTIME._project_human_decision(
            task_id=task_id,
            target=target,
            decision=decision,
        )
        _RUNTIME.store.save_hds_confirmation_projection(resolved)
        pending_decisions = [
            item
            for item in _RUNTIME.human_decisions.list(task_id=task_id)
            if item.status
            in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
            }
        ]
        if not pending_decisions:
            if task.status in {
                RadarTaskStatus.WAITING_FOR_CONFIRMATION,
                RadarTaskStatus.RUNNING,
            }:
                _RUNTIME.resume_product_after_confirmations(task_id)
            elif not exact_retry:
                raise InvalidTaskTransition(
                    "terminal confirmation replay is not authoritative"
                )
            resolved = _RUNTIME.store.get_confirmation(resolved.confirmation_id)
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
        decision = _RUNTIME.human_decisions.get(decision_id)
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
            for item in _RUNTIME.store.list_confirmations(task_id)
            if item.decision_id == decision_id
        ]
        if len(projections) != 1:
            raise InvalidTaskTransition(
                "decision has no unique task confirmation projection"
            )
        checkpoint = _RUNTIME._latest_product_checkpoint(task_id)
        targets = [
            PendingConfirmationTarget.model_validate(item)
            for item in checkpoint.payload.get("pending_confirmations", [])
        ]
        matching_targets = [
            item
            for item in targets
            if _product_confirmation_id(task_id, item)
            == projections[0].confirmation_id
        ]
        if len(matching_targets) != 1:
            raise InvalidTaskTransition(
                "decision projection has no unique frozen target"
            )
        bound_target = matching_targets[0]
        if bound_target.decision_id != decision_id:
            raise InvalidTaskTransition(
                "decision projection does not match the frozen HDS binding"
            )
        _RUNTIME._bound_human_decision(
            task_id=task_id,
            target=bound_target,
        )
        _RUNTIME.store.save_hds_confirmation_projection(
            _RUNTIME._project_human_decision(
                task_id=task_id,
                target=bound_target,
                decision=revoked,
            )
        )
        pending_decisions = [
            item
            for item in _RUNTIME.human_decisions.list(task_id=task_id)
            if item.status
            in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
            }
        ]
        if (
            not pending_decisions
            and task.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION
        ):
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
) -> list[dict[str, Any]]:
    snapshots = [item for item in artifacts if item.artifact_type == "workflow_run"]
    if not snapshots:
        return []
    latest = max(snapshots, key=lambda item: (item.version, item.created_at))
    values = latest.payload.get("questionnaire_candidates", [])
    return [dict(item) for item in values if isinstance(item, dict)][:3]


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


def _task_status_value(task: RadarAgentTask | HistoricalTaskRecord) -> str:
    status = task.status
    return status.value if isinstance(status, RadarTaskStatus) else str(status)


def _event_payload(event: RadarTaskEvent | HistoricalRecord) -> dict[str, Any]:
    return (
        event.to_dict()
        if isinstance(event, HistoricalRecord)
        else event.model_dump(mode="json")
    )


def _event_type(event: RadarTaskEvent | HistoricalRecord) -> str:
    return str(_event_payload(event).get("event_type", ""))


def _decision_trace_entry(
    event: RadarTaskEvent | HistoricalRecord,
) -> dict[str, Any]:
    persisted = _event_payload(event)
    details = persisted.get("payload", {})
    if not isinstance(details, dict):
        details = {}
    allowed_details = {
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
    return {
        "sequence": persisted.get("sequence"),
        "event_type": persisted.get("event_type"),
        "summary": persisted.get("message"),
        "created_at": persisted.get("created_at"),
        "details": {
            key: value for key, value in details.items() if key in allowed_details
        },
    }


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


def _format_sse_event(event: RadarTaskEvent | HistoricalRecord) -> str:
    payload = _event_payload(event)
    return (
        f"id: {payload.get('sequence', '')}\n"
        f"event: {payload.get('event_type', 'historical.event')}\n"
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
    "HumanDecisionPublicView",
    "RadarApiRuntime",
    "RadarChatRequest",
    "RadarChatResponse",
    "RadarTaskCreateRequest",
    "RadarTaskDetail",
    "get_radar_api_runtime",
    "reset_radar_api_runtime_for_tests",
    "router",
]
