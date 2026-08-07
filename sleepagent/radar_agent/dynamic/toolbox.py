from __future__ import annotations

from datetime import date
from typing import Any

from sleepagent.radar_agent.agents.risk import RiskSignalAgent
from sleepagent.radar_agent.agents.trend import TrendAgent
from sleepagent.radar_agent.confirmation import confirmation_request
from sleepagent.radar_agent.dynamic.contracts import FactSnapshot, GoalType, UserGoal
from sleepagent.radar_agent.evidence import EvidenceLedgerBuilder
from sleepagent.radar_agent.quality import DataQualityGate, RadarProviderLike
from sleepagent.radar_agent.questionnaire.defaults import (
    DEFAULT_QUESTIONNAIRE_BANK,
    DEFAULT_SKILL_QUESTION_PACK,
)
from sleepagent.radar_agent.rag import default_seed_knowledge_store
from sleepagent.radar_agent.runtime import RadarAgentTask, TaskService
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireEntry,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)

from .model import AgentWorkProduct
from .tool_registry import ALLOWED_DYNAMIC_TOOLS, DYNAMIC_TOOL_DEFINITIONS


SAFETY_NOTICES = [
    "本报告由 AI 辅助整理，内容来自结构化证据。",
    "本报告仅用于睡眠健康观察。",
    "本报告不构成临床诊断或医疗建议。",
    "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
]


class DynamicToolbox:
    """Deterministic capability tools used only through the Orchestrator."""

    def __init__(
        self,
        *,
        provider: RadarProviderLike,
        service: TaskService,
    ) -> None:
        self.provider = provider
        self.service = service
        self.quality_gate = DataQualityGate()
        self.trend = TrendAgent()
        self.risk = RiskSignalAgent()
        self.rag = default_seed_knowledge_store()

    def execute(
        self,
        tool_name: str,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in ALLOWED_DYNAMIC_TOOLS:
            raise PermissionError(f"tool is not allowed in dynamic runtime: {tool_name}")
        method_name = "_" + tool_name.replace(".", "_")
        method = getattr(self, method_name, None)
        if method is None:
            raise NotImplementedError(f"dynamic tool is not configured: {tool_name}")
        return method(task=task, goal=goal, state=state)

    @staticmethod
    def definition(tool_name: str):
        try:
            return DYNAMIC_TOOL_DEFINITIONS[tool_name]
        except KeyError as exc:
            raise PermissionError(
                f"tool is not registered in the dynamic runtime: {tool_name}"
            ) from exc

    def _radar_read(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        device = self.provider.get_device(task.radar_device_id).model_copy(
            update={
                "bound_subject_id": task.subject_id,
                "source_raw_event_ids": [f"device-source:{task.radar_device_id}"],
            }
        )
        snapshots = [
            item.model_copy(
                update={
                    "subject_id": task.subject_id,
                    "radar_device_id": task.radar_device_id,
                    "source_raw_event_ids": [f"snapshot-source:{item.snapshot_id}"],
                }
            )
            for item in self.provider.pull_snapshots(task.radar_device_id)
        ]
        provider_summary = self.provider.pull_night_report(
            task.radar_device_id, night_of=goal.target_date
        )
        if provider_summary is not None:
            provider_summary = provider_summary.model_copy(
                update={
                    "subject_id": task.subject_id,
                    "radar_device_id": task.radar_device_id,
                    "source_raw_event_ids": [
                        f"snapshot-source:{snapshot.snapshot_id}"
                        for snapshot in snapshots
                    ],
                    "source_report_ref": (
                        f"provider-night:{task.radar_device_id}:"
                        f"{provider_summary.night_of.isoformat()}"
                    ),
                }
            )
        return {
            "device": device,
            "snapshots": snapshots,
            "provider_night_summary": provider_summary,
        }

    def _quality_assess(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        read = _required(state, "radar.read")
        summary = self.quality_gate.run(
            device=read["device"],
            snapshots=read["snapshots"],
            provider_report=read.get("provider_night_summary"),
            night_of=goal.target_date,
        )
        self.service.store.save_night_summary(summary)
        return {"night_summary": summary}

    def _history_query(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        if goal.goal_type == GoalType.GROUNDED_QUESTION:
            artifacts = self.service.store.list_artifact_versions_by_artifact_id(
                goal.source_artifact_id
            )
            if not artifacts:
                raise KeyError("explicit source artifact is not available")
            source_task = self.service.get_task(artifacts[-1].task_id)
            if source_task.subject_id != task.subject_id:
                raise PermissionError("explicit source artifact belongs to another subject")
            source_ledgers = [
                item.evidence_ledger
                for item in self.service.list_artifacts(source_task.task_id)
                if item.evidence_ledger is not None
                and item.evidence_ledger.derived_metrics.get("night_of")
                == goal.source_date.isoformat()
            ]
            if not source_ledgers:
                raise KeyError("explicit source artifact has no evidence for source_date")
            return {
                "source_artifact_id": goal.source_artifact_id,
                "source_date": goal.source_date,
                "artifacts": artifacts,
                "source_ledger": source_ledgers[-1],
            }
        summaries = self.service.store.list_night_summaries(task.subject_id)
        if goal.range_start is not None:
            summaries = [item for item in summaries if item.night_of >= goal.range_start]
        if goal.range_end is not None:
            summaries = [item for item in summaries if item.night_of <= goal.range_end]
        current = state.get("quality.assess", {}).get("night_summary")
        if current is not None and all(item.night_of != current.night_of for item in summaries):
            summaries.append(current)
        return {"summaries": sorted(summaries, key=lambda item: item.night_of)}

    def _trend_calculate(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        history = _required(state, "history.query")
        result = self.trend.analyze(task_id=task.task_id, summaries=history["summaries"])
        return {"trend_result": result}

    def _urgent_boundary_evaluate(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        summary = state.get("quality.assess", {}).get("night_summary")
        trend_result = state.get("trend.calculate", {}).get("trend_result")
        claims = list(getattr(trend_result, "claims", []))
        text_inputs = [value for value in (goal.focus, goal.question) if value]
        decision = self.risk.analyze(
            task_id=task.task_id,
            night_summary=summary,
            trend_claims=claims,
            text_inputs=text_inputs,
        )
        return {"risk_decision": decision}

    def _rag_retrieve_reviewed(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(
            value
            for value in (goal.question, goal.focus, goal.goal_type.value)
            if value
        )
        chunks = self.rag.search(
            role=task.role,
            reviewed_only=True,
            query=query,
            limit=8,
        )
        return {
            "rag_context": {
                "chunk_ids": [item.chunk_id for item in chunks],
                "citation_ids": [item.citation_id for item in chunks],
                "snippets": [item.content for item in chunks],
                "caveats": [note for item in chunks for note in item.safety_notes],
                "source_metadata": [
                    {
                        "chunk_id": item.chunk_id,
                        "citation_id": item.citation_id,
                        "version": item.version,
                        "source_type": item.source_type,
                        "review_status": item.review_status.value,
                    }
                    for item in chunks
                ],
            }
        }

    def _questionnaire_select(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        requested_id = state.get("requested_question_id")
        questions = {
            **DEFAULT_QUESTIONNAIRE_BANK.questions,
            **DEFAULT_SKILL_QUESTION_PACK.questions,
        }
        question = questions.get(requested_id)
        if question is None or task.role not in question.applicable_roles:
            return {"question": None}
        return {"question": question}

    def _questionnaire_capture(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        responses = state.get("user_input_responses", [])
        return {"responses": responses}

    def _evidence_ledger_validate(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        builder = EvidenceLedgerBuilder(
            ledger_id=f"dynamic-ledger:{task.task_id}", task_id=task.task_id
        )
        summary: RadarNightSummary | None = state.get("quality.assess", {}).get("night_summary")
        trend_result = state.get("trend.calculate", {}).get("trend_result")
        risk_decision = state.get("urgent_boundary.evaluate", {}).get("risk_decision")
        if summary is not None:
            ref = _summary_ref(summary)
            builder.add_canonical_ref(ref)
            builder.add_metric("night_of", summary.night_of.isoformat())
            builder.add_metric("data_quality_status", summary.data_quality_status.value)
            builder.add_metric("data_coverage_ratio", summary.data_coverage_ratio)
            builder.add_metric("total_sleep_minutes", summary.total_sleep_minutes)
            builder.add_metric("out_of_bed_count", summary.out_of_bed_count)
            builder.add_metric("movement_count", summary.movement_count)
            builder.add_claim(
                EvidenceClaim(
                    claim_id=f"claim:{task.task_id}:night-facts",
                    task_id=task.task_id,
                    text=_night_fact_text(summary),
                    evidence_refs=[ref],
                    confidence=max(0.1, min(1.0, summary.data_coverage_ratio)),
                    risk_level=RiskLevel.INFO,
                    uncertainty=(
                        "data_quality_not_interpretable"
                        if summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
                        else None
                    ),
                    caveats=list(summary.caveats),
                    generated_by="quality.assess",
                    review_status=ReviewStatus.REVIEWED,
                )
            )
        if trend_result is not None:
            for ref in trend_result.evidence_refs:
                builder.add_canonical_ref(ref)
            for claim in trend_result.claims:
                builder.add_claim(claim.model_copy(update={"review_status": ReviewStatus.REVIEWED}))
            builder.add_metric("trend_result", trend_result.model_dump(mode="json"))
        if risk_decision is not None:
            builder.add_metric("risk_level", risk_decision.risk_level.value)
            for ref in risk_decision.evidence_refs:
                builder.add_canonical_ref(ref)
            for claim in risk_decision.claims:
                builder.add_claim(claim.model_copy(update={"review_status": ReviewStatus.REVIEWED}))
        rag_context = state.get("rag.retrieve_reviewed", {}).get("rag_context")
        if rag_context is not None:
            for ref in rag_context.get("citation_ids", []):
                builder.add_canonical_ref(ref)
            for caveat in rag_context.get("caveats", []):
                builder.add_caveat(caveat)
            builder.add_metric(
                "reviewed_rag",
                {
                    "citation_ids": rag_context.get("citation_ids", []),
                    "source_metadata": rag_context.get("source_metadata", []),
                },
            )
        products: list[AgentWorkProduct] = state.get("agent_products", [])
        source_ledger = state.get("history.query", {}).get("source_ledger")
        if source_ledger is not None:
            for ref in source_ledger.canonical_evidence_refs:
                builder.add_canonical_ref(ref)
            for claim in source_ledger.claims:
                builder.add_claim(claim.model_copy(update={"task_id": task.task_id}))
            builder.add_metric(
                "explicit_source",
                {
                    "artifact_id": goal.source_artifact_id,
                    "source_date": goal.source_date.isoformat(),
                    "ledger_id": source_ledger.ledger_id,
                },
            )
        questions = {
            **DEFAULT_QUESTIONNAIRE_BANK.questions,
            **DEFAULT_SKILL_QUESTION_PACK.questions,
        }
        for response in state.get("user_input_responses", []):
            if response.answered_by_role == "system":
                continue
            request = self.service.store.get_user_input_request(response.request_id)
            question = questions.get(request.question_id)
            if question is None:
                continue
            from_skill = request.question_id in DEFAULT_SKILL_QUESTION_PACK.questions
            source_id = (
                DEFAULT_SKILL_QUESTION_PACK.skill_id
                if from_skill
                else DEFAULT_QUESTIONNAIRE_BANK.bank_id
            )
            source_version = (
                DEFAULT_SKILL_QUESTION_PACK.version
                if from_skill
                else DEFAULT_QUESTIONNAIRE_BANK.version
            )
            builder.add_questionnaire_entry(
                QuestionnaireEntry(
                    entry_id=f"self-report:{response.response_id}",
                    subject_id=task.subject_id,
                    role=response.answered_by_role,
                    question_id=request.question_id,
                    answer=response.answer,
                    answer_type=question.answer_type,
                    collected_at=response.created_at,
                    source="micro_questionnaire",
                    question_source="skill" if from_skill else "bank",
                    source_id=source_id,
                    source_version=source_version,
                    policy_id="dynamic-reviewed-user-input",
                    policy_version="1.0.0",
                    trigger="agent_evidence_gap",
                    prompt_text=request.question_text,
                    evidence_ref=f"self-report:{response.response_id}",
                )
            )
        builder.add_metric(
            "agent_summaries",
            [
                {
                    "summary": product.summary,
                    "confidence": product.confidence,
                    "uncertainties": product.uncertainties,
                }
                for product in products
            ],
        )
        builder.add_caveat("Observational sleep support only; no diagnosis is produced.")
        ledger = builder.build()
        # Validation is explicit and complete before commit/render.
        EvidenceLedger.model_validate(ledger.model_dump(mode="python"))
        return {"ledger": ledger}

    def _evidence_ledger_commit(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        ledger: EvidenceLedger = _required(state, "evidence.ledger_validate")["ledger"]
        snapshot = FactSnapshot.create(
            snapshot_id=f"facts:{task.task_id}:{ledger.ledger_id}",
            task_id=task.task_id,
            source_refs=ledger.canonical_evidence_refs,
            facts={
                "ledger_id": ledger.ledger_id,
                "derived_metrics": ledger.derived_metrics,
                "claims": [claim.model_dump(mode="json") for claim in ledger.claims],
                "questionnaire_entries": [
                    entry.model_dump(mode="json")
                    for entry in ledger.questionnaire_entries
                ],
            },
        )
        self.service.store.save_fact_snapshot(snapshot)
        artifact_version = self.service.save_artifact(
            task.task_id,
            ledger,
            metadata={
                "runtime_kind": "dynamic_goal",
                "goal_id": goal.goal_id,
                "execution_mode": task.execution_mode,
                "fact_snapshot_id": snapshot.snapshot_id,
                "fact_snapshot_sha256": snapshot.sha256,
            },
        )
        return {
            "ledger": ledger,
            "artifact_version_id": artifact_version.artifact_version_id,
            "fact_snapshot": snapshot,
        }

    def _role_artifact_render(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        ledger: EvidenceLedger = _required(state, "evidence.ledger_commit")["ledger"]
        artifact = _render_role_artifact(task=task, goal=goal, ledger=ledger, state=state)
        version = self.service.save_artifact(
            task.task_id,
            artifact,
            metadata=_artifact_metadata(task=task, goal=goal, state=state),
        )
        return {"artifact": artifact, "artifact_version_id": version.artifact_version_id}

    def _doctor_material_render(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        ledger: EvidenceLedger = _required(state, "evidence.ledger_commit")["ledger"]
        artifact = _render_role_artifact(
            task=task.model_copy(update={"role": "doctor"}),
            goal=goal,
            ledger=ledger,
            state=state,
        )
        version = self.service.save_artifact(
            task.task_id,
            artifact,
            metadata=_artifact_metadata(task=task, goal=goal, state=state),
        )
        return {"artifact": artifact, "artifact_version_id": version.artifact_version_id}

    def _confirmation_request(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        ledger: EvidenceLedger = _required(state, "evidence.ledger_commit")["ledger"]
        request = confirmation_request(
            task_id=task.task_id,
            action_type="export_doctor_material",
            evidence_refs=ledger.canonical_evidence_refs,
            reason="导出或分享医生材料前需要由相应角色确认。",
        )
        saved = self.service.request_confirmation(task.task_id, request)
        return {"confirmation": saved}

    def _confirmed_action_execute(self, *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]) -> dict[str, Any]:
        confirmation_id = state.get("approved_confirmation_id")
        if not confirmation_id:
            raise PermissionError("confirmed action requires an approved confirmation")
        exported = self.service.export_doctor_material(
            task.task_id,
            confirmation_id=confirmation_id,
        )
        return {
            "action_type": "export_doctor_material",
            "artifact_version_id": exported.artifact_version_id,
            "artifact_id": exported.artifact_id,
        }


def _required(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"required dynamic tool result is missing: {key}")
    return value


def _artifact_metadata(
    *, task: RadarAgentTask, goal: UserGoal, state: dict[str, Any]
) -> dict[str, Any]:
    snapshot = _required(state, "evidence.ledger_commit")["fact_snapshot"]
    return {
        "runtime_kind": "dynamic_goal",
        "goal_id": goal.goal_id,
        "execution_mode": task.execution_mode,
        "deterministic_fallback": task.execution_mode == "safe_degraded",
        "fact_snapshot_id": snapshot.snapshot_id,
        "fact_snapshot_sha256": snapshot.sha256,
        "source_ledger_id": _required(state, "evidence.ledger_commit")[
            "ledger"
        ].ledger_id,
    }


def _summary_ref(summary: RadarNightSummary) -> str:
    return summary.source_report_ref or (
        f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"
    )


def _night_fact_text(summary: RadarNightSummary) -> str:
    if summary.data_quality_status == RadarDataQualityStatus.UNUSABLE:
        return f"{summary.night_of.isoformat()} 的雷达数据质量不足，不能进行睡眠趋势解释。"
    minutes = "未记录" if summary.total_sleep_minutes is None else f"{summary.total_sleep_minutes:.0f} 分钟"
    return (
        f"{summary.night_of.isoformat()} 的可用记录覆盖率为 "
        f"{summary.data_coverage_ratio:.0%}，睡眠时长为 {minutes}，离床 {summary.out_of_bed_count} 次。"
    )


def _render_role_artifact(
    *, task: RadarAgentTask, goal: UserGoal, ledger: EvidenceLedger, state: dict[str, Any]
) -> RoleReportArtifact:
    role = task.role if task.role in {"elder", "family", "doctor"} else "family"
    risk_value = str(ledger.derived_metrics.get("risk_level", "info"))
    try:
        risk = RiskLevel(risk_value)
    except ValueError:
        risk = RiskLevel.UNCERTAIN
    products: list[AgentWorkProduct] = state.get("agent_products", [])
    if products:
        content = "\n".join(product.summary for product in products)
    elif goal.goal_type == GoalType.CHANGE_EXPLANATION:
        content = "已整理可观察到的变化；当前降级模式不推断变化原因。"
    elif goal.goal_type == GoalType.GROUNDED_QUESTION:
        content = "当前仅能返回明确来源材料中的结构化事实。"
    else:
        content = "已按所选日期整理雷达记录与数据质量结果。"
    if goal.target_date is not None:
        scope_text = f"观察日期：{goal.target_date.isoformat()}（{goal.resolved_timezone_name}）"
    elif goal.range_start is not None and goal.range_end is not None:
        scope_text = (
            f"观察范围：{goal.range_start.isoformat()} 至 {goal.range_end.isoformat()}"
            f"（{goal.resolved_timezone_name}）"
        )
    else:
        scope_text = (
            f"来源日期：{goal.source_date.isoformat()}（{goal.resolved_timezone_name}）"
        )
    content = f"{scope_text}\n{content}"
    evidence_refs = list(
        dict.fromkeys(
            ledger.canonical_evidence_refs
            + [ref for claim in ledger.claims for ref in claim.evidence_refs]
        )
    )
    data_quality = {
        key: ledger.derived_metrics.get(key)
        for key in ("data_quality_status", "data_coverage_ratio")
        if key in ledger.derived_metrics
    }
    caveats = list(dict.fromkeys([*ledger.caveats, "仅供连续观察，不替代医生判断。"]))
    structured = {
        "source_ledger_id": ledger.ledger_id,
        "risk_level": risk.value,
        "claim_ids": [claim.claim_id for claim in ledger.claims],
        "execution_mode": task.execution_mode,
        "resolved_timezone_name": goal.resolved_timezone_name,
        "target_date": goal.target_date,
        "range_start": goal.range_start,
        "range_end": goal.range_end,
        "source_date": goal.source_date,
    }
    snapshot = state.get("evidence.ledger_commit", {}).get("fact_snapshot")
    if snapshot is not None:
        structured.update(
            {
                "fact_snapshot_id": snapshot.snapshot_id,
                "fact_snapshot_sha256": snapshot.sha256,
            }
        )
    if role == "doctor":
        structured.update(
            {
                "evidence_chain": [claim.model_dump(mode="json") for claim in ledger.claims],
                "data_quality": data_quality,
                "questionnaire_entries": [
                    item.model_dump(mode="json") for item in ledger.questionnaire_entries
                ],
                "source_refs": evidence_refs,
                "caveats": caveats,
                "safety_notices": SAFETY_NOTICES,
                "non_diagnostic_boundary": True,
            }
        )
    return RoleReportArtifact(
        artifact_id=f"dynamic-report:{task.task_id}:{role}",
        task_id=task.task_id,
        role=role,
        title={
            GoalType.NIGHT_REVIEW: "指定夜间观察",
            GoalType.TREND_COMPARISON: "睡眠趋势对比",
            GoalType.CHANGE_EXPLANATION: "变化说明",
            GoalType.DATA_QUALITY_DIAGNOSIS: "设备与数据质量说明",
            GoalType.DOCTOR_MATERIAL: "医生审阅材料",
            GoalType.GROUNDED_QUESTION: "基于指定材料的回答",
        }[goal.goal_type],
        content=content,
        source_ledger_id=ledger.ledger_id,
        risk_level=risk,
        claim_ids=[claim.claim_id for claim in ledger.claims],
        facts=list(ledger.claims),
        evidence_refs=evidence_refs,
        source_refs=evidence_refs,
        data_quality=data_quality,
        questionnaire_entries=list(ledger.questionnaire_entries),
        structured_summary=structured,
        caveats=caveats,
        safety_notices=SAFETY_NOTICES,
        prompt_version="dynamic-role-render.v1",
        model_provider="deterministic-renderer",
        model_id="dynamic-role-renderer-v1",
        generation_mode="template",
    )


def json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


__all__ = ["DynamicToolbox", "SAFETY_NOTICES", "json_safe"]
