from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, ValidationError

from sleepagent.product_device.api import (
    DEFAULT_FAKE_RADAR_DEVICE_ID,
    FakeRadarProductDataProvider,
    build_public_alert,
    build_public_dashboard,
    build_public_sleep_report,
    scenario_trend_summary,
)
from sleepagent.product_device.dialogue import (
    ProductDialogueValidationError,
    check_product_dialogue_output_safety,
)
from sleepagent.product_device.llm import (
    DEFAULT_PRODUCT_LLM_BASE_URL,
    DEFAULT_PRODUCT_LLM_MODEL,
    OpenAICompatibleChatProvider,
    OpenAICompatibleProviderConfig,
    PRODUCT_LLM_API_KEY_ENV,
    ProductChatProvider,
    ProductLLMNotConfiguredError,
    ProductLLMProviderError,
)
from sleepagent.product_device.schemas import ProductDeviceSchema
from sleepagent.radar_agent.replay import default_replay_scenario_id

if TYPE_CHECKING:
    from sleepagent.radar_agent.product_agent import ProductEpisodeRunner


RADAR_AGENT_SCHEMA_VERSION = "radar_sleep_agent_run.v1"
RADAR_AGENT_DEMO_FIXTURE_VERSION = "radar-demo-v2-2026-07-08"
DEFAULT_RADAR_AGENT_QUESTION = "昨晚睡得怎么样？"


class RadarRunSchema(ProductDeviceSchema):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AgentEventType(str, Enum):
    TASK_CREATED = "task_created"
    STEP_STARTED = "step_started"
    TOOL_CALLED = "tool_called"
    FINDING_CREATED = "finding_created"
    ARTIFACT_CREATED = "artifact_created"
    TASK_COMPLETED = "task_completed"
    ERROR = "error"


class ArtifactType(str, Enum):
    ELDER_REPORT = "elder_report"
    FAMILY_REPORT = "family_report"
    DOCTOR_REPORT = "doctor_report"


class ArtifactStatus(str, Enum):
    DRAFT = "draft"


class AgentEvent(RadarRunSchema):
    id: str = Field(..., min_length=1)
    type: AgentEventType
    step_id: str | None = Field(default=None, alias="stepId", min_length=1)
    title: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] | None = None


class Artifact(RadarRunSchema):
    id: str = Field(..., min_length=1)
    task_id: str | None = Field(default=None, alias="taskId", min_length=1)
    subject_id: str | None = Field(default=None, alias="subjectId", min_length=1)
    record_id: str | None = Field(default=None, alias="recordId", min_length=1)
    type: ArtifactType
    title: str = Field(..., min_length=1)
    status: ArtifactStatus
    content: str = Field(..., min_length=1)
    created_by_step_id: str = Field(..., alias="createdByStepId", min_length=1)
    current_version_id: str | None = Field(
        default=None,
        alias="currentVersionId",
        min_length=1,
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="updatedAt",
    )


class RadarAgentRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


class RadarAgentRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_LLM = "failed_llm"
    FAILED_VALIDATION = "failed_validation"
    LINEAR_FALLBACK = "linear_fallback"


class RadarAgentStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RadarAgentStep(ProductDeviceSchema):
    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)
    status: RadarAgentStepStatus = RadarAgentStepStatus.PENDING
    source: str = Field(..., min_length=1)


class RadarAgentEvidence(ProductDeviceSchema):
    evidence_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    source_type: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)


class RadarSleepKnowledgeChunk(ProductDeviceSchema):
    chunk_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    source_type: Literal["internal_seed", "reviewed_reference"]
    safety_notes: list[str] = Field(default_factory=list)


class RadarAgentArtifactDraft(ProductDeviceSchema):
    title: str = Field(..., min_length=1)
    headline: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    observations: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class RadarAgentDeepSeekDraft(ProductDeviceSchema):
    schema_version: Literal["radar_sleep_agent_run.v1"] = RADAR_AGENT_SCHEMA_VERSION
    evidence_ids: list[str] = Field(default_factory=list)
    elder_artifact: RadarAgentArtifactDraft
    family_artifact: RadarAgentArtifactDraft
    doctor_artifact: RadarAgentArtifactDraft
    shared_caveats: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)


class RadarAgentChatTurn(ProductDeviceSchema):
    id: str = Field(..., min_length=1)
    role: RadarAgentRole
    user_message: str = Field(..., min_length=1)
    assistant_message: str = Field(..., min_length=1)
    caveats: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarAgentRunCreateRequest(ProductDeviceSchema):
    radar_device_id: str = Field(default=DEFAULT_FAKE_RADAR_DEVICE_ID, min_length=1)
    question: str = Field(default=DEFAULT_RADAR_AGENT_QUESTION, min_length=1)
    role: RadarAgentRole = RadarAgentRole.FAMILY
    scenario: str = Field(default_factory=default_replay_scenario_id, min_length=1)
    locale: str = "zh-CN"
    idempotency_key: str | None = Field(default=None, min_length=1)


class RadarAgentRoleRequest(ProductDeviceSchema):
    role: RadarAgentRole


class RadarAgentAskRequest(ProductDeviceSchema):
    user_message: str = Field(..., min_length=1)
    role: RadarAgentRole = RadarAgentRole.FAMILY


class RadarAgentRun(ProductDeviceSchema):
    run_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    scenario: str = Field(default_factory=default_replay_scenario_id, min_length=1)
    selected_role: RadarAgentRole
    status: RadarAgentRunStatus
    graph_mode: str = Field(..., min_length=1)
    data_mode: Literal["demo"] = "demo"
    fixture_version: str = RADAR_AGENT_DEMO_FIXTURE_VERSION
    generated_from_demo_data: bool = True
    visible_steps: list[RadarAgentStep] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    evidence: list[RadarAgentEvidence] = Field(default_factory=list)
    knowledge: list[RadarSleepKnowledgeChunk] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    current_artifact: Artifact | None = None
    dashboard: dict[str, Any] | None = None
    realtime: dict[str, Any] | None = None
    sleep_report: dict[str, Any] | None = None
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    trend_summary: list[dict[str, Any]] = Field(default_factory=list)
    scenario_expectations: dict[str, Any] = Field(default_factory=dict)
    chat_turns: list[RadarAgentChatTurn] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    error_message: str | None = None
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InMemoryRadarAgentRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RadarAgentRun] = {}
        self._idempotency_index: dict[str, str] = {}

    def get_by_idempotency_key(self, key: str | None) -> RadarAgentRun | None:
        if not key:
            return None
        run_id = self._idempotency_index.get(key)
        if run_id is None:
            return None
        return self._runs.get(run_id)

    def save(self, run: RadarAgentRun) -> RadarAgentRun:
        self._runs[run.run_id] = run
        if run.idempotency_key:
            self._idempotency_index[run.idempotency_key] = run.run_id
        return run

    def get(self, run_id: str) -> RadarAgentRun:
        return self._runs[run_id]


class RadarSleepAgentService:
    def __init__(
        self,
        *,
        data_provider: FakeRadarProductDataProvider,
        store: InMemoryRadarAgentRunStore | None = None,
        episode_runner: "ProductEpisodeRunner",
    ) -> None:
        self.data_provider = data_provider
        self.store = store or InMemoryRadarAgentRunStore()
        self.episode_runner = episode_runner

    def create_run(
        self,
        request: RadarAgentRunCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> RadarAgentRun:
        resolved_key = request.idempotency_key or idempotency_key or build_idempotency_key(
            radar_device_id=request.radar_device_id,
            question=request.question,
            scenario=request.scenario,
        )
        existing = self.store.get_by_idempotency_key(resolved_key)
        if existing is not None:
            return existing.model_copy(update={"selected_role": request.role})

        created_at = datetime.now(timezone.utc)
        run = RadarAgentRun(
            run_id=f"radar-run-{uuid.uuid4().hex[:12]}",
            radar_device_id=request.radar_device_id,
            question=request.question,
            scenario=request.scenario,
            selected_role=request.role,
            status=RadarAgentRunStatus.RUNNING,
            graph_mode="product_episode_runner",
            visible_steps=_visible_steps(RadarAgentStepStatus.RUNNING),
            events=[
                _event(
                    event_type=AgentEventType.TASK_CREATED,
                    title="雷达复盘任务已创建",
                    message="SleepAgent 开始自动复盘昨夜睡眠。",
                    timestamp=created_at,
                    payload={
                        "data_mode": "demo",
                        "fixture_version": RADAR_AGENT_DEMO_FIXTURE_VERSION,
                        "scenario": request.scenario,
                    },
                )
            ],
            idempotency_key=resolved_key,
            created_at=created_at,
            updated_at=created_at,
        )
        self.store.save(run)
        completed = self._run_graph(run)
        return self.store.save(completed)

    def get_run(self, run_id: str) -> RadarAgentRun:
        return self.store.get(run_id)

    def set_role(self, run_id: str, role: RadarAgentRole) -> RadarAgentRun:
        run = self.store.get(run_id)
        current_artifact = _artifact_for_role(run.artifacts, role)
        updated = run.model_copy(
            update={
                "selected_role": role,
                "current_artifact": current_artifact,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self.store.save(updated)

    def ask(self, run_id: str, request: RadarAgentAskRequest) -> RadarAgentRun:
        run = self.set_role(run_id, request.role)
        return self._ask_with_product_episode_runner(run, request)

    def _run_graph(self, run: RadarAgentRun) -> RadarAgentRun:
        return self._run_product_episode_graph(run)

    def _run_product_episode_graph(self, run: RadarAgentRun) -> RadarAgentRun:
        from sleepagent.radar_agent.product_agent import (
            EpisodeStatus,
            EpisodeType,
        )

        runner = self.episode_runner
        assert runner is not None
        data_provider = self._data_provider_for_scenario(run.scenario)
        dashboard = data_provider.build_dashboard(run.radar_device_id)
        public_dashboard = build_public_dashboard(dashboard)
        sleep_report = data_provider.get_latest_sleep_report(run.radar_device_id)
        alerts = data_provider.get_recent_alerts(run.radar_device_id)
        realtime = data_provider.get_realtime(run.radar_device_id)
        evidence = build_radar_evidence(dashboard)
        knowledge = retrieve_radar_sleep_knowledge()
        trend_summary = scenario_trend_summary(data_provider.scenario)
        scenario_expectations = data_provider.scenario.expected.model_dump(mode="json")
        caveats = [
            "当前为演示雷达数据。",
            "内容由四角色 ProductEpisodeRunner 生成并通过确定性发布门。",
            "内容仅用于睡眠观察，不构成医学诊断。",
            *dashboard.caveats,
        ]
        common = {
            "evidence": evidence,
            "knowledge": knowledge,
            "dashboard": public_dashboard.model_dump(mode="json"),
            "realtime": realtime.model_dump(mode="json"),
            "sleep_report": (
                build_public_sleep_report(sleep_report).model_dump(mode="json")
                if sleep_report is not None
                else None
            ),
            "alerts": [
                build_public_alert(alert).model_dump(mode="json") for alert in alerts
            ],
            "trend_summary": trend_summary,
            "scenario_expectations": scenario_expectations,
            "caveats": caveats,
        }
        if not _episode_runner_is_configured(runner):
            return run.model_copy(
                update={
                    **common,
                    "status": RadarAgentRunStatus.FAILED_LLM,
                    "graph_mode": "product_episode_runner",
                    "visible_steps": _visible_steps(RadarAgentStepStatus.FAILED),
                    "events": [
                        *run.events,
                        _event(
                            event_type=AgentEventType.ERROR,
                            step_id="role-response",
                            title="DeepSeek API 未配置",
                            message=(
                                "请在后端环境变量中配置 DEEPSEEK_API_KEY 后重试；"
                                "旧单模型生成链不会作为回退路径。"
                            ),
                        ),
                    ],
                    "error_message": "DeepSeek API 未配置，请设置 DEEPSEEK_API_KEY。",
                    "updated_at": datetime.now(timezone.utc),
                }
            )

        artifacts: list[Artifact] = []
        episode_events: list[AgentEvent] = [
            *run.events,
            _event(
                event_type=AgentEventType.STEP_STARTED,
                step_id="intent",
                title="理解问题",
                message=f"四角色运行时开始处理：{run.question}",
            ),
            _event(
                event_type=AgentEventType.TOOL_CALLED,
                step_id="radar-data",
                title="读取演示雷达数据",
                message="确定性 Tool 已构造固定 FactSnapshot 和雷达证据输入。",
            ),
        ]
        failure_codes: list[str] = []
        safety_flags: list[str] = []
        for role in RadarAgentRole:
            episode_request = self._product_episode_request(
                run=run,
                dashboard=dashboard,
                role=role,
                user_text=run.question,
                episode_type=EpisodeType.ROLE_MATERIAL,
                doctor_material=role == RadarAgentRole.DOCTOR,
                episode_suffix=f"material:{role.value}",
            )
            result = runner.run(episode_request)
            publication = result.publication
            failure_codes.extend(result.receipt.failure_codes)
            if result.receipt.safety_decision_refs:
                safety_flags.append(f"{role.value}:safety_reviewed")
            episode_events.append(
                _event(
                    event_type=AgentEventType.FINDING_CREATED,
                    step_id="role-response",
                    title=f"{_role_label(role)}材料四角色执行完成",
                    message=(
                        f"Episode {result.receipt.episode_id} 状态："
                        f"{result.receipt.status.value}。"
                    ),
                    payload={
                        "episode_id": result.receipt.episode_id,
                        "episode_status": result.receipt.status.value,
                        "agent_invocation_ids": result.receipt.agent_invocation_ids,
                        "tool_receipt_ids": result.receipt.tool_receipt_ids,
                        "accepted_work_product_refs": (
                            result.receipt.accepted_work_product_refs
                        ),
                        "safety_decision_refs": (
                            result.receipt.safety_decision_refs
                        ),
                    },
                )
            )
            if (
                publication is None
                or result.receipt.status == EpisodeStatus.BLOCKED
                or publication.audience_role != role.value
            ):
                if (
                    publication is not None
                    and publication.audience_role != role.value
                ):
                    failure_codes.append("communication_audience_role_mismatch")
                continue
            now = datetime.now(timezone.utc)
            artifacts.append(
                Artifact(
                    id=f"artifact-{run.run_id}-{role.value}",
                    taskId=run.run_id,
                    subjectId=run.radar_device_id,
                    recordId=result.receipt.episode_id,
                    type=_artifact_type_for_role(role),
                    title=_artifact_title_for_role(role),
                    status=ArtifactStatus.DRAFT,
                    content=publication.text,
                    createdByStepId="role-response",
                    currentVersionId=publication.draft_id,
                    createdAt=now,
                    updatedAt=now,
                )
            )
            if publication.context_notice not in caveats:
                caveats.append(publication.context_notice)

        current = _artifact_for_role(artifacts, run.selected_role)
        if current is None:
            return run.model_copy(
                update={
                    **common,
                    "status": RadarAgentRunStatus.FAILED_VALIDATION,
                    "graph_mode": "product_episode_runner",
                    "visible_steps": _visible_steps(RadarAgentStepStatus.FAILED),
                    "events": [
                        *episode_events,
                        _event(
                            event_type=AgentEventType.ERROR,
                            step_id="artifact",
                            title="四角色发布门未生成当前角色材料",
                            message="未启用旧 JSON 生成链回退。",
                            payload={"failure_codes": list(dict.fromkeys(failure_codes))},
                        ),
                    ],
                    "safety_flags": list(dict.fromkeys(safety_flags)),
                    "error_message": "四角色运行时未生成可发布的当前角色材料。",
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        completed = [
            *episode_events,
            _event(
                event_type=AgentEventType.ARTIFACT_CREATED,
                step_id="artifact",
                title="角色材料已生成",
                message="角色材料全部来自 ProductEpisodeRunner 的已验收 Communication。",
                payload={"artifact_ids": [item.id for item in artifacts]},
            ),
            _event(
                event_type=AgentEventType.TASK_COMPLETED,
                title="雷达复盘完成",
                message="SleepAgent 四角色运行时已完成昨夜睡眠复盘。",
            ),
        ]
        return run.model_copy(
            update={
                **common,
                "status": RadarAgentRunStatus.COMPLETED,
                "graph_mode": "product_episode_runner",
                "visible_steps": _visible_steps(RadarAgentStepStatus.COMPLETED),
                "events": completed,
                "artifacts": artifacts,
                "current_artifact": current,
                "caveats": caveats,
                "safety_flags": list(dict.fromkeys(safety_flags)),
                "error_message": None,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _ask_with_product_episode_runner(
        self,
        run: RadarAgentRun,
        request: RadarAgentAskRequest,
    ) -> RadarAgentRun:
        from sleepagent.radar_agent.product_agent import EpisodeType

        runner = self.episode_runner
        assert runner is not None
        if not _episode_runner_is_configured(runner):
            turn = RadarAgentChatTurn(
                id=f"radar-chat-{uuid.uuid4().hex[:10]}",
                role=request.role,
                user_message=request.user_message,
                assistant_message="DeepSeek API 未配置，暂时无法生成追问回答。",
                caveats=["旧单模型追问链不会作为回退路径。"],
            )
            return self.store.save(
                run.model_copy(
                    update={
                        "chat_turns": [*run.chat_turns, turn],
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            )
        dashboard = self._data_provider_for_scenario(run.scenario).build_dashboard(
            run.radar_device_id
        )
        result = runner.run(
            self._product_episode_request(
                run=run,
                dashboard=dashboard,
                role=request.role,
                user_text=request.user_message,
                episode_type=EpisodeType.GROUNDED_DIALOGUE,
                doctor_material=False,
                episode_suffix=f"followup:{len(run.chat_turns) + 1}",
            )
        )
        publication = result.publication
        turn = RadarAgentChatTurn(
            id=f"radar-chat-{uuid.uuid4().hex[:10]}",
            role=request.role,
            user_message=request.user_message,
            assistant_message=(
                publication.text
                if publication is not None
                else "当前无法可靠完成这次解释，请稍后重试。"
            ),
            caveats=(
                [publication.context_notice]
                if publication is not None
                else list(result.receipt.failure_codes)
            ),
        )
        event = _event(
            event_type=AgentEventType.FINDING_CREATED,
            title="追问回答已生成",
            message="追问已由 ProductEpisodeRunner 生成并通过发布门。",
            payload={
                "role": request.role.value,
                "episode_id": result.receipt.episode_id,
                "episode_status": result.receipt.status.value,
                "agent_invocation_ids": result.receipt.agent_invocation_ids,
            },
        )
        return self.store.save(
            run.model_copy(
                update={
                    "chat_turns": [*run.chat_turns, turn],
                    "events": [*run.events, event],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )

    def _product_episode_request(
        self,
        *,
        run: RadarAgentRun,
        dashboard: Any,
        role: RadarAgentRole,
        user_text: str,
        episode_type: Any,
        doctor_material: bool,
        episode_suffix: str,
    ) -> Any:
        from sleepagent.radar_agent.product_agent import (
            AuthenticatedBinding,
            ClaimKind,
            FactSnapshot,
            ProductEpisodeRunRequest,
            SourceScope,
            SourceScopeKind,
            build_unavailable_entry_decisions,
            snapshot_binding_material,
            stable_hash,
        )

        runner = self.episode_runner
        assert runner is not None
        generated_at = dashboard.generated_at
        timezone_name = os.getenv("SLEEPAGENT_PRODUCT_TIMEZONE", "Asia/Shanghai")
        local_date = generated_at.astimezone(ZoneInfo(timezone_name)).date()
        actor_id = os.getenv("SLEEPAGENT_PRODUCT_ACTOR_ID")
        subject_id = os.getenv("SLEEPAGENT_PRODUCT_SUBJECT_ID")
        actor_role = os.getenv("SLEEPAGENT_PRODUCT_ACTOR_ROLE")
        if not actor_id or not subject_id or actor_role not in {
            "elder",
            "family",
            "doctor",
        }:
            raise ProductDialogueValidationError(
                "Product actor binding is not configured."
            )
        source_ref = (
            f"product-radar:{run.radar_device_id}:{generated_at.isoformat()}"
        )
        scope = SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=generated_at,
            timezone_name=timezone_name,
            date_start=local_date,
            date_end=local_date,
            # Multi-metric dialogue uses per-metric readiness decisions; the
            # legacy SourceScope scalar must not imply global maturity.
            valid_night_count=0,
        )
        canonical_payload = dashboard.model_dump(mode="json")
        claim_kind = (
            ClaimKind.GENERAL_KNOWLEDGE
            if episode_type.value == "grounded_dialogue"
            else ClaimKind.DESCRIBE_CURRENT_NIGHT
        )
        readiness_decisions = build_unavailable_entry_decisions(
            decision_namespace=f"{run.run_id}:{episode_suffix}:cold-start",
            claim_kind=claim_kind,
        )
        snapshot = FactSnapshot.create(
            fact_snapshot_id=(
                f"product-run:{stable_hash((run.run_id, episode_suffix))[:20]}"
            ),
            binding=AuthenticatedBinding(
                actor_id=actor_id,
                subject_id=subject_id,
                role=actor_role,
                authorization_scope=(
                    "read_sleep_data",
                    "read_device_data",
                    "draft_material",
                ),
            ),
            source_scope=scope,
            canonical_data_version=stable_hash(canonical_payload),
            care_context_version=runner.commit_controller.care_store.get(
                subject_id
            ).version,
            memory_context_version=runner.commit_controller.memory_store.get(
                subject_id
            ).version,
            source_refs=(source_ref,),
            **snapshot_binding_material(decisions=readiness_decisions),
            created_at=generated_at,
        )
        return ProductEpisodeRunRequest(
            episode_id=f"{run.run_id}:{episode_suffix}",
            episode_type=episode_type,
            objective=(
                f"基于已授权雷达信息，为{_role_label(role)}生成受证据约束的内容"
            ),
            fact_snapshot=snapshot,
            runtime_readiness_decisions=readiness_decisions,
            user_text=user_text,
            audience_role=role.value,
            personalized=True,
            doctor_material=doctor_material,
            tool_inputs={
                "radar.get_night_evidence": {
                    "data": canonical_payload,
                    "source_refs": [source_ref],
                },
                "radar.assess_data_quality": {
                    "coverage_ratio": (
                        0.0
                        if dashboard.data_quality.blocks_current_values
                        else 1.0
                    ),
                    "source_refs": [source_ref],
                },
            },
        )

    def _data_provider_for_scenario(self, scenario: str) -> FakeRadarProductDataProvider:
        if getattr(self.data_provider, "scenario_id", None) == scenario:
            return self.data_provider
        if (
            os.getenv("SLEEPAGENT_RADAR_AGENT_DEV_MODE", "false")
            .strip()
            .lower()
            != "true"
            or os.getenv("SLEEPAGENT_DEPLOYMENT_MODE", "development")
            .strip()
            .lower()
            == "production"
            or os.getenv("SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE", "")
            .strip()
            .lower()
            != "fake"
        ):
            raise RuntimeError(
                "scenario FakeRadarProductDataProvider is development/test only"
            )
        return FakeRadarProductDataProvider(scenario)


def build_radar_agent_llm_config_from_env() -> OpenAICompatibleProviderConfig:
    return OpenAICompatibleProviderConfig(
        model=os.getenv("SLEEPAGENT_PRODUCT_LLM_MODEL", DEFAULT_PRODUCT_LLM_MODEL),
        api_key_env=PRODUCT_LLM_API_KEY_ENV,
        base_url=os.getenv(
            "SLEEPAGENT_PRODUCT_LLM_BASE_URL", DEFAULT_PRODUCT_LLM_BASE_URL
        ),
        timeout_seconds=float(
            os.getenv("SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS", "30")
        ),
        max_output_tokens=int(os.getenv("SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS", "1600")),
        temperature=0.2,
    )


def build_idempotency_key(
    *,
    radar_device_id: str,
    question: str,
    scenario: str | None = None,
) -> str:
    normalized_question = " ".join(question.strip().split())
    resolved_scenario = scenario or default_replay_scenario_id()
    return (
        f"{radar_device_id}:{resolved_scenario}:2026-07-08:{normalized_question}:"
        f"{RADAR_AGENT_DEMO_FIXTURE_VERSION}"
    )


def build_radar_evidence(dashboard) -> list[RadarAgentEvidence]:
    snapshot = dashboard.current_snapshot
    report = dashboard.latest_sleep_report
    alerts = dashboard.recent_alerts
    evidence: list[RadarAgentEvidence] = []
    if snapshot is not None:
        evidence.extend(
            [
                RadarAgentEvidence(
                    evidence_id="current_bed_presence",
                    label="当前在床状态",
                    value="在床" if snapshot.bed_presence.value == "in_bed" else "离床",
                    source_type="demo_radar_snapshot",
                    detail=f"测量时间 {snapshot.measured_at.isoformat()}",
                ),
                RadarAgentEvidence(
                    evidence_id="current_vitals",
                    label="当前生命体征",
                    value=(
                        f"心率 {snapshot.heart_rate_bpm or '无'} bpm，"
                        f"呼吸 {snapshot.breath_rate_bpm or '无'} bpm"
                    ),
                    source_type="demo_radar_snapshot",
                    detail="来自演示雷达快照。",
                ),
            ]
        )
    if report is not None:
        evidence.extend(
            [
                RadarAgentEvidence(
                    evidence_id="sleep_score",
                    label="昨夜睡眠评分",
                    value=f"{report.sleep_score or '无'}",
                    source_type="demo_sleep_report",
                    detail="供应商睡眠报告评分，仅作观察。",
                ),
                RadarAgentEvidence(
                    evidence_id="sleep_duration",
                    label="昨夜总睡眠",
                    value=f"{round(report.total_sleep_minutes or 0)} 分钟",
                    source_type="demo_sleep_report",
                    detail=(
                        f"{report.sleep_start_at.isoformat() if report.sleep_start_at else '未知'}"
                        " 至 "
                        f"{report.sleep_end_at.isoformat() if report.sleep_end_at else '未知'}"
                    ),
                ),
                RadarAgentEvidence(
                    evidence_id="stage_summary",
                    label="睡眠分期摘要",
                    value=(
                        f"深睡 {round(report.deep_sleep_minutes or 0)} 分钟，"
                        f"REM {round(report.rem_sleep_minutes or 0)} 分钟"
                    ),
                    source_type="demo_sleep_report",
                    detail="雷达分期不等同于 PSG 分期。",
                ),
                RadarAgentEvidence(
                    evidence_id="movement_getup",
                    label="体动与离床",
                    value=(
                        f"体动 {report.movement_count or 0} 次，"
                        f"离床 {report.getup_count or 0} 次"
                    ),
                    source_type="demo_sleep_report",
                    detail="用于生活观察和照护提醒。",
                ),
            ]
        )
    evidence.append(
        RadarAgentEvidence(
            evidence_id="alert_summary",
            label="近期告警",
            value=f"{len(alerts)} 条，其中 {sum(1 for item in alerts if item.resolved_at is None)} 条未处理",
            source_type="demo_alerts",
            detail="包含离床和体动提醒。",
        )
    )
    evidence.append(
        RadarAgentEvidence(
            evidence_id="data_quality",
            label="数据质量",
            value="可用于演示观察",
            source_type="demo_quality",
            detail="当前数据为演示数据，不是实时设备数据。",
        )
    )
    return evidence


def retrieve_radar_sleep_knowledge() -> list[RadarSleepKnowledgeChunk]:
    return [
        RadarSleepKnowledgeChunk(
            chunk_id="radar-non-diagnostic-boundary",
            title="雷达睡眠数据边界",
            summary="毫米波雷达可用于观察在床、体动、呼吸和睡眠趋势，但不能替代 PSG 或医生诊断。",
            source_type="internal_seed",
            safety_notes=["避免诊断性结论。"],
        ),
        RadarSleepKnowledgeChunk(
            chunk_id="sleep-regularity-care",
            title="作息观察建议",
            summary="连续观察睡眠时长、离床次数和白天精神状态，比单晚评分更适合用于家庭照护决策。",
            source_type="internal_seed",
            safety_notes=["建议保持温和，不给治疗或用药建议。"],
        ),
        RadarSleepKnowledgeChunk(
            chunk_id="urgent-symptom-boundary",
            title="急症边界",
            summary="如出现胸痛、严重呼吸困难、意识异常等急性症状，应及时线下评估或急诊处理。",
            source_type="internal_seed",
            safety_notes=["急症不能用生活建议替代。"],
        ),
    ]


def build_role_artifact_messages(
    *,
    run: RadarAgentRun,
    evidence: list[RadarAgentEvidence],
    knowledge: list[RadarSleepKnowledgeChunk],
    caveats: list[str],
    trend_summary: list[dict[str, Any]],
) -> list[dict[str, str]]:
    artifact_schema = {
        "title": "string",
        "headline": "string",
        "summary": "string",
        "observations": ["string"],
        "suggested_actions": ["string"],
        "caveats": ["string"],
        "evidence_ids": ["one or more ids from evidence[].evidence_id"],
    }
    payload = {
        "question": run.question,
        "locale": "zh-CN",
        "data_mode": "demo",
        "fixture_version": RADAR_AGENT_DEMO_FIXTURE_VERSION,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "knowledge": [item.model_dump(mode="json") for item in knowledge],
        "trend_summary": trend_summary,
        "caveats": caveats,
        "required_schema": {
            "schema_version": RADAR_AGENT_SCHEMA_VERSION,
            "evidence_ids": ["all referenced evidence ids"],
            "elder_artifact": artifact_schema,
            "family_artifact": artifact_schema,
            "doctor_artifact": artifact_schema,
            "shared_caveats": ["demo data", "DeepSeek generated", "not diagnosis"],
            "safety_flags": ["optional strings"],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 SleepAgent 的雷达睡眠复盘助手。只输出 JSON。"
                "基于给定演示雷达证据生成老人版、家属版、医生版三个 Artifact。"
                "三版必须引用同一组证据，不得编造未给出的指标。"
                "family_artifact 和 doctor_artifact 必须是对象，结构必须与 elder_artifact 完全一致。"
                "evidence_ids 只能使用 evidence[].evidence_id 中出现过的 id。"
                "不得做诊断、用药、治疗或急诊分诊。必须说明演示数据、DeepSeek 生成、非医学诊断。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_followup_messages(
    run: RadarAgentRun,
    request: RadarAgentAskRequest,
) -> list[dict[str, str]]:
    payload = {
        "role": request.role.value,
        "user_message": request.user_message,
        "evidence": [item.model_dump(mode="json") for item in run.evidence],
        "knowledge": [item.model_dump(mode="json") for item in run.knowledge],
        "caveats": run.caveats,
        "required_schema": {"answer": "string", "caveats": ["string"]},
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 SleepAgent 的雷达睡眠追问助手。只输出 JSON。"
                "回答必须符合当前角色视角，只能使用提供的证据和知识边界。"
                "不得诊断、治疗或给药。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _load_radar_agent_json(raw_json: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_json, dict):
        return raw_json
    text = raw_json.decode("utf-8") if isinstance(raw_json, bytes) else str(raw_json)
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError("Radar agent output must be a JSON object.")
    return payload


def _normalize_radar_agent_payload(
    payload: dict[str, Any],
    evidence: list[RadarAgentEvidence],
) -> dict[str, Any]:
    evidence_ids = [item.evidence_id for item in evidence]
    default_evidence_ids = evidence_ids[:4]
    normalized: dict[str, Any] = {
        "schema_version": RADAR_AGENT_SCHEMA_VERSION,
        "evidence_ids": _coerce_str_list(payload.get("evidence_ids"))
        or default_evidence_ids,
        "shared_caveats": _coerce_str_list(payload.get("shared_caveats")),
        "safety_flags": _coerce_str_list(payload.get("safety_flags")),
    }
    role_sources = {
        "elder_artifact": (
            payload.get("elder_artifact")
            or payload.get("elder")
            or payload.get("老人版")
        ),
        "family_artifact": (
            payload.get("family_artifact")
            or payload.get("family")
            or payload.get("家属版")
        ),
        "doctor_artifact": (
            payload.get("doctor_artifact")
            or payload.get("doctor")
            or payload.get("医生版")
        ),
    }
    defaults = {
        "elder_artifact": ("老人版睡眠解释", "昨晚整体可观察，今天按平常节奏留意身体感受。"),
        "family_artifact": ("家属版照护摘要", "昨晚没有直接高危结论，建议继续观察离床和体动变化。"),
        "doctor_artifact": ("医生版数据摘要", "本摘要整理雷达演示数据指标和数据质量边界。"),
    }
    for key, raw_artifact in role_sources.items():
        if not isinstance(raw_artifact, dict):
            normalized[key] = raw_artifact
            continue
        title, fallback_summary = defaults[key]
        normalized[key] = _normalize_artifact_payload(
            raw_artifact,
            default_title=title,
            default_summary=fallback_summary,
            default_evidence_ids=default_evidence_ids,
        )
    if not normalized["shared_caveats"]:
        normalized["shared_caveats"] = [
            "当前为演示雷达数据。",
            "解释由 DeepSeek 根据演示证据生成。",
            "内容仅用于睡眠观察，不构成医学诊断。",
        ]
    referenced = []
    for key in ("elder_artifact", "family_artifact", "doctor_artifact"):
        artifact = normalized.get(key)
        if isinstance(artifact, dict):
            referenced.extend(_coerce_str_list(artifact.get("evidence_ids")))
    if referenced:
        normalized["evidence_ids"] = _dedupe(referenced)
    return normalized


def _normalize_artifact_payload(
    payload: dict[str, Any],
    *,
    default_title: str,
    default_summary: str,
    default_evidence_ids: list[str],
) -> dict[str, Any]:
    title = _coerce_str(payload.get("title")) or default_title
    headline = (
        _coerce_str(payload.get("headline"))
        or _coerce_str(payload.get("conclusion"))
        or _coerce_str(payload.get("结论"))
        or default_summary
    )
    summary = (
        _coerce_str(payload.get("summary"))
        or _coerce_str(payload.get("answer"))
        or _coerce_str(payload.get("摘要"))
        or default_summary
    )
    return {
        "title": title,
        "headline": headline,
        "summary": summary,
        "observations": _coerce_str_list(
            payload.get("observations")
            or payload.get("findings")
            or payload.get("观察依据")
        ),
        "suggested_actions": _coerce_str_list(
            payload.get("suggested_actions")
            or payload.get("actions")
            or payload.get("建议动作")
        ),
        "caveats": _coerce_str_list(
            payload.get("caveats")
            or payload.get("说明")
        ),
        "evidence_ids": _coerce_str_list(
            payload.get("evidence_ids")
            or payload.get("evidenceIds")
            or payload.get("证据编号")
        )
        or default_evidence_ids,
    }


def build_fallback_role_draft(
    *,
    evidence: list[RadarAgentEvidence],
    caveats: list[str],
    validation_error: str,
) -> RadarAgentDeepSeekDraft:
    values = {item.evidence_id: item.value for item in evidence}
    sleep_score = values.get("sleep_score", "暂无")
    sleep_duration = values.get("sleep_duration", "暂无")
    movement_getup = values.get("movement_getup", "暂无")
    alert_summary = values.get("alert_summary", "暂无")
    current_vitals = values.get("current_vitals", "暂无")
    stage_summary = values.get("stage_summary", "暂无")
    data_quality = values.get("data_quality", "可观察")
    shared_caveats = _dedupe(
        [
            *caveats,
            "DeepSeek 输出未通过结构或安全校验，本结果为 SleepAgent 规则化安全兜底。",
            "内容仅用于睡眠观察，不构成医学诊断。",
        ]
    )
    return RadarAgentDeepSeekDraft(
        evidence_ids=[item.evidence_id for item in evidence],
        elder_artifact=RadarAgentArtifactDraft(
            title="老人版睡眠解释",
            headline="昨晚整体可以作为一次平稳的演示观察。",
            summary=(
                f"演示数据里，昨夜睡眠评分为 {sleep_score}，总睡眠约 {sleep_duration}。"
                "今天可以按平常节奏活动，重点留意白天精神状态。"
            ),
            observations=[
                f"昨夜总睡眠：{sleep_duration}。",
                f"体动与离床：{movement_getup}。",
            ],
            suggested_actions=[
                "今天保持正常作息，白天如果明显困倦再记录下来。",
                "今晚继续保持固定上床时间，睡前减少刺激性活动。",
            ],
            caveats=shared_caveats,
            evidence_ids=["sleep_score", "sleep_duration", "movement_getup"],
        ),
        family_artifact=RadarAgentArtifactDraft(
            title="家属版照护摘要",
            headline="昨晚未见直接高危结论，但离床、体动和告警需要继续观察。",
            summary=(
                f"演示数据中当前生命体征为 {current_vitals}，近期告警为 {alert_summary}。"
                "建议家属连续观察几晚，而不是只根据单晚评分判断。"
            ),
            observations=[
                f"当前生命体征：{current_vitals}。",
                f"近期告警：{alert_summary}。",
                f"体动与离床：{movement_getup}。",
            ],
            suggested_actions=[
                "关注夜间离床次数是否连续增加。",
                "如果白天精神状态明显下降，可把连续多晚记录带给医生参考。",
            ],
            caveats=shared_caveats,
            evidence_ids=["current_vitals", "alert_summary", "movement_getup"],
        ),
        doctor_artifact=RadarAgentArtifactDraft(
            title="医生版数据摘要",
            headline="雷达演示报告显示睡眠评分、时长、分期和告警均可追溯到同一证据包。",
            summary=(
                f"睡眠评分 {sleep_score}，总睡眠 {sleep_duration}，分期摘要为 {stage_summary}。"
                f"数据质量标记为：{data_quality}。"
            ),
            observations=[
                f"睡眠评分：{sleep_score}。",
                f"总睡眠：{sleep_duration}。",
                f"分期摘要：{stage_summary}。",
                f"数据质量：{data_quality}。",
            ],
            suggested_actions=[
                "结合主诉、病史、白天功能状态和连续趋势判断。",
                "如需临床判断，应结合线下评估和标准检查。",
            ],
            caveats=shared_caveats,
            evidence_ids=["sleep_score", "sleep_duration", "stage_summary", "data_quality"],
        ),
        shared_caveats=shared_caveats,
        safety_flags=["llm_output_validation_fallback", validation_error],
    )


def validate_radar_agent_deepseek_json(
    raw_json: str | bytes | dict[str, Any],
    evidence: list[RadarAgentEvidence],
) -> RadarAgentDeepSeekDraft:
    try:
        payload = _normalize_radar_agent_payload(
            _load_radar_agent_json(raw_json),
            evidence,
        )
        draft = RadarAgentDeepSeekDraft.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ProductDialogueValidationError("Radar agent output failed JSON validation.") from exc

    allowed_evidence_ids = {item.evidence_id for item in evidence}
    for artifact in [
        draft.elder_artifact,
        draft.family_artifact,
        draft.doctor_artifact,
    ]:
        if not set(artifact.evidence_ids).issubset(allowed_evidence_ids):
            raise ProductDialogueValidationError("Radar agent output referenced unknown evidence.")
        safety = check_product_dialogue_output_safety(
            _radar_safety_text(
                " ".join(
                    [
                        artifact.headline,
                        artifact.summary,
                        *artifact.observations,
                        *artifact.suggested_actions,
                        *artifact.caveats,
                    ]
                )
            )
        )
        if not safety.passed:
            raise ProductDialogueValidationError(
                "Radar agent output failed safety validation: "
                + ",".join(safety.blocked_reasons)
            )
    return draft


def build_role_artifacts(
    *,
    run_id: str,
    radar_device_id: str,
    draft: RadarAgentDeepSeekDraft,
    shared_caveats: list[str],
) -> list[Artifact]:
    now = datetime.now(timezone.utc)
    mapping = [
        (RadarAgentRole.ELDER, ArtifactType.ELDER_REPORT, draft.elder_artifact),
        (RadarAgentRole.FAMILY, ArtifactType.FAMILY_REPORT, draft.family_artifact),
        (RadarAgentRole.DOCTOR, ArtifactType.DOCTOR_REPORT, draft.doctor_artifact),
    ]
    artifacts: list[Artifact] = []
    for role, artifact_type, artifact_draft in mapping:
        content = _artifact_content(
            role=role,
            draft=artifact_draft,
            shared_caveats=[*shared_caveats, *draft.shared_caveats],
        )
        artifacts.append(
            Artifact(
                id=f"radar-artifact-{role.value}-{uuid.uuid4().hex[:10]}",
                taskId=run_id,
                subjectId="radar-demo-user",
                recordId=radar_device_id,
                type=artifact_type,
                title=artifact_draft.title,
                status=ArtifactStatus.DRAFT,
                content=content,
                createdByStepId="role-response",
                currentVersionId=f"radar-version-{uuid.uuid4().hex[:10]}",
                createdAt=now,
                updatedAt=now,
            )
        )
    return artifacts


def _artifact_content(
    *,
    role: RadarAgentRole,
    draft: RadarAgentArtifactDraft,
    shared_caveats: list[str],
) -> str:
    sections = [
        draft.headline,
        "",
        draft.summary,
    ]
    if draft.observations:
        sections.extend(["", "观察依据：", *[f"- {item}" for item in draft.observations]])
    if draft.suggested_actions:
        title = "今天可以做：" if role == RadarAgentRole.ELDER else "建议动作："
        sections.extend(["", title, *[f"- {item}" for item in draft.suggested_actions]])
    caveats = _dedupe([*draft.caveats, *shared_caveats])
    if caveats:
        sections.extend(["", "说明：", *[f"- {item}" for item in caveats]])
    if draft.evidence_ids:
        sections.extend(["", f"证据编号：{', '.join(draft.evidence_ids)}"])
    return "\n".join(sections)


def _visible_steps(status: RadarAgentStepStatus) -> list[RadarAgentStep]:
    labels = [
        ("intent", "理解问题", "识别昨夜睡眠复盘意图", "IntentNode"),
        ("radar-data", "读取雷达睡眠数据", "加载演示快照、报告、告警和趋势", "RadarDataNode"),
        ("quality", "检查数据质量", "确认数据新鲜度和演示数据边界", "DataQualityNode"),
        ("rag", "检索睡眠知识", "加载非诊断边界和照护建议", "SleepInsightNode + RadarSleepRagNode"),
        ("response", "生成角色化解释", "调用 DeepSeek 生成三角色 Artifact", "RoleResponseNode + ArtifactNode"),
    ]
    return [
        RadarAgentStep(id=item_id, label=label, detail=detail, status=status, source=source)
        for item_id, label, detail, source in labels
    ]


def _failed_run(
    *,
    run: RadarAgentRun,
    status: RadarAgentRunStatus,
    events: list[AgentEvent],
    evidence: list[RadarAgentEvidence],
    knowledge: list[RadarSleepKnowledgeChunk],
    dashboard: dict[str, Any],
    realtime: dict[str, Any],
    sleep_report: dict[str, Any] | None,
    alerts: list[dict[str, Any]],
    trend_summary: list[dict[str, Any]],
    scenario_expectations: dict[str, Any],
    caveats: list[str],
    message: str,
    detail: str,
) -> RadarAgentRun:
    return run.model_copy(
        update={
            "status": status,
            "visible_steps": _visible_steps(RadarAgentStepStatus.FAILED),
            "events": [
                *events,
                _event(
                    event_type=AgentEventType.ERROR,
                    step_id="role-response",
                    title=message,
                    message=detail,
                ),
            ],
            "evidence": evidence,
            "knowledge": knowledge,
            "dashboard": dashboard,
            "realtime": realtime,
            "sleep_report": sleep_report,
            "alerts": alerts,
            "trend_summary": trend_summary,
            "scenario_expectations": scenario_expectations,
            "caveats": caveats,
            "error_message": message,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def _artifact_for_role(
    artifacts: list[Artifact],
    role: RadarAgentRole,
) -> Artifact | None:
    target_type = {
        RadarAgentRole.ELDER: ArtifactType.ELDER_REPORT,
        RadarAgentRole.FAMILY: ArtifactType.FAMILY_REPORT,
        RadarAgentRole.DOCTOR: ArtifactType.DOCTOR_REPORT,
    }[role]
    return next((artifact for artifact in artifacts if artifact.type == target_type), None)


def _artifact_type_for_role(role: RadarAgentRole) -> ArtifactType:
    return {
        RadarAgentRole.ELDER: ArtifactType.ELDER_REPORT,
        RadarAgentRole.FAMILY: ArtifactType.FAMILY_REPORT,
        RadarAgentRole.DOCTOR: ArtifactType.DOCTOR_REPORT,
    }[role]


def _artifact_title_for_role(role: RadarAgentRole) -> str:
    return {
        RadarAgentRole.ELDER: "老人版睡眠解释",
        RadarAgentRole.FAMILY: "家属版照护摘要",
        RadarAgentRole.DOCTOR: "医生版就诊材料草稿",
    }[role]


def _event(
    *,
    event_type: AgentEventType,
    title: str,
    message: str,
    step_id: str | None = None,
    timestamp: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    return AgentEvent(
        id=f"radar-event-{uuid.uuid4().hex[:10]}",
        type=event_type,
        stepId=step_id,
        title=title,
        message=message,
        timestamp=timestamp or datetime.now(timezone.utc),
        payload=payload,
    )


def _provider_is_configured(provider: ProductChatProvider) -> bool:
    return not hasattr(provider, "is_configured") or bool(provider.is_configured)


def _episode_runner_is_configured(runner: Any) -> bool:
    from sleepagent.radar_agent.product_agent.agents import ProductAgentRoster

    roster = getattr(runner, "agent_roster", None)
    if type(roster) is not ProductAgentRoster:
        return False
    models = [
        roster.sleepcare.planning_model,
        *(item.model for item in roster),
    ]
    return all(bool(getattr(model, "is_configured", True)) for model in models)


def _role_label(role: RadarAgentRole) -> str:
    return {
        RadarAgentRole.ELDER: "老人版",
        RadarAgentRole.FAMILY: "家属版",
        RadarAgentRole.DOCTOR: "医生版",
    }[role]


def _demo_trend_summary() -> list[dict[str, Any]]:
    return [
        {"date": "07/02", "sleep_score": 78, "total_sleep_minutes": 392, "getup_count": 2},
        {"date": "07/03", "sleep_score": 80, "total_sleep_minutes": 410, "getup_count": 1},
        {"date": "07/04", "sleep_score": 76, "total_sleep_minutes": 380, "getup_count": 2},
        {"date": "07/05", "sleep_score": 83, "total_sleep_minutes": 421, "getup_count": 1},
        {"date": "07/06", "sleep_score": 79, "total_sleep_minutes": 398, "getup_count": 2},
        {"date": "07/07", "sleep_score": 81, "total_sleep_minutes": 407, "getup_count": 1},
        {"date": "07/08", "sleep_score": 82, "total_sleep_minutes": 405, "getup_count": 1},
    ]


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            deduped.append(cleaned)
            seen.add(cleaned)
    return deduped


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [text for item in value if (text := _coerce_str(item))]
    text = _coerce_str(value)
    return [text] if text else []


def _radar_safety_text(text: str) -> str:
    allowed_disclaimers = (
        "不构成医学诊断",
        "不构成诊断",
        "不是医学诊断",
        "不是诊断",
        "非医疗诊断",
        "非医学诊断",
        "不能替代医学诊断",
        "不能替代诊断",
        "不能替代医生诊断",
        "不能替代临床诊断",
        "非医学诊断",
        "非诊断",
        "非 PSG 结果",
        "非PSG结果",
        "不等同 PSG",
        "不等同PSG",
        "不能替代 PSG",
        "不能替代PSG",
        "不等同于 PSG",
        "不能替代临床检查",
        "不替代临床检查",
        "不能替代线下评估",
        "不替代线下评估",
        "不能替代多导睡眠",
        "不等同于多导睡眠",
        "如需临床判断，应结合线下评估和标准检查",
        "结合线下评估和标准检查",
        "及时线下评估",
        "急诊处理",
        "胸痛",
        "严重呼吸困难",
        "意识异常",
    )
    cleaned = text
    for disclaimer in allowed_disclaimers:
        cleaned = cleaned.replace(disclaimer, "安全边界说明")
    return cleaned
