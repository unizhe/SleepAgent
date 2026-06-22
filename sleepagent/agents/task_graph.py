from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable, TypedDict

from sleepagent.agents.langgraph_orchestration import (
    _load_langgraph_symbols,
)
from sleepagent.agents.report_agent import ReportAgent
from sleepagent.schemas.analysis import AnalysisNodeResult, AnalysisNodeStatus
from sleepagent.schemas.report import MockSleepReport, RetrievedReportKnowledgeChunk
from sleepagent.schemas.task import (
    AgentEvent,
    AgentEventType,
    AgentPlanStep,
    AgentPlanStepStatus,
    Artifact,
    ArtifactStatus,
    ArtifactType,
    NextAction,
    NextActionType,
    SleepAgentTask,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
)
from sleepagent.services.analysis_service import AnalysisService
from sleepagent.services.report_retrievers import retrieve_report_context


LOAD_RECORD_NODE = "load_record"
QUALITY_CHECK_NODE = "quality_check"
SLEEP_STAGING_NODE = "sleep_staging"
RESPIRATORY_DETECTION_NODE = "respiratory_detection"
RISK_ASSESSMENT_NODE = "risk_assessment"
MEDICAL_RAG_NODE = "medical_rag"
REPORT_GENERATION_NODE = "report_generation"
CARE_PLAN_NODE = "care_plan"
CHAT_CONTEXT_NODE = "chat_context"

TASK_GRAPH_NODE_ORDER = [
    LOAD_RECORD_NODE,
    QUALITY_CHECK_NODE,
    SLEEP_STAGING_NODE,
    RESPIRATORY_DETECTION_NODE,
    RISK_ASSESSMENT_NODE,
    MEDICAL_RAG_NODE,
    REPORT_GENERATION_NODE,
    CARE_PLAN_NODE,
    CHAT_CONTEXT_NODE,
]

MEDICAL_DISCLAIMER = (
    "SleepAgent 输出仅用于睡眠健康辅助分析和科研原型展示，不能替代医生诊断、"
    "治疗建议或急救判断。若出现胸痛、严重呼吸困难、意识异常等情况，请及时就医。"
)


class SleepAgentGraphState(TypedDict, total=False):
    task: SleepAgentTask
    analysis_run: Any
    report: MockSleepReport
    rag_chunks: list[RetrievedReportKnowledgeChunk]
    events: list[AgentEvent]
    artifacts: list[Artifact]
    errors: list[str]


GraphNode = Callable[[SleepAgentGraphState], SleepAgentGraphState]


def build_default_task_plan() -> list[AgentPlanStep]:
    return [
        AgentPlanStep(
            id=LOAD_RECORD_NODE,
            title="解析本地睡眠记录",
            agent="SleepAnalysisAgent",
            description="解析 SHHS EDF/XML 路径并确认本地数据角色。",
        ),
        AgentPlanStep(
            id=QUALITY_CHECK_NODE,
            title="信号质量检查",
            agent="SleepAnalysisAgent",
            description="检查通道、采样率、时长和基础可用性。",
        ),
        AgentPlanStep(
            id=SLEEP_STAGING_NODE,
            title="YASA 睡眠分期",
            agent="SleepAnalysisAgent",
            description="运行 YASA adapter 并生成 Wake/REM/NREM 摘要。",
        ),
        AgentPlanStep(
            id=RESPIRATORY_DETECTION_NODE,
            title="呼吸事件检测",
            agent="SleepAnalysisAgent",
            description="优先使用 SHHS 标注摘要，未验证模型只作为 caveat。",
        ),
        AgentPlanStep(
            id=RISK_ASSESSMENT_NODE,
            title="风险评估和证据链",
            agent="SleepAnalysisAgent",
            description="汇总 AHI、事件统计、模型可信度和风险等级。",
        ),
        AgentPlanStep(
            id=MEDICAL_RAG_NODE,
            title="医学知识检索",
            agent="ReportAgent",
            description="检索内部种子知识并保留安全边界说明。",
        ),
        AgentPlanStep(
            id=REPORT_GENERATION_NODE,
            title="生成多角色报告",
            agent="ReportAgent",
            description="生成老人、家属、医生和技术说明 Artifact。",
        ),
        AgentPlanStep(
            id=CARE_PLAN_NODE,
            title="生成观察计划草稿",
            agent="ReportAgent",
            description="生成需要用户确认后启用的观察计划 Artifact。",
        ),
        AgentPlanStep(
            id=CHAT_CONTEXT_NODE,
            title="构建对话上下文",
            agent="DialogueAgent",
            description="整理本次任务上下文、后续动作和安全提示。",
        ),
    ]


def build_sleep_agent_task_langgraph(
    *,
    analysis_service: AnalysisService | None = None,
    report_agent: ReportAgent | None = None,
) -> Any:
    state_graph, end = _load_langgraph_symbols()
    graph = state_graph(SleepAgentGraphState)
    nodes = _build_task_graph_nodes(
        analysis_service=analysis_service,
        report_agent=report_agent,
    )

    for node_name in TASK_GRAPH_NODE_ORDER:
        graph.add_node(node_name, nodes[node_name])
    graph.set_entry_point(LOAD_RECORD_NODE)
    for source, target in zip(TASK_GRAPH_NODE_ORDER, TASK_GRAPH_NODE_ORDER[1:]):
        graph.add_edge(source, target)
    graph.add_edge(CHAT_CONTEXT_NODE, end)
    return graph.compile()


def run_sleep_agent_task_state_graph(
    task: SleepAgentTask,
    *,
    analysis_service: AnalysisService | None = None,
    report_agent: ReportAgent | None = None,
    prefer_langgraph: bool = False,
) -> SleepAgentTask:
    if prefer_langgraph:
        app = build_sleep_agent_task_langgraph(
            analysis_service=analysis_service,
            report_agent=report_agent,
        )
        final_state = app.invoke(_initial_state(task))
    else:
        final_state = _run_direct_state_graph(
            _initial_state(task),
            analysis_service=analysis_service,
            report_agent=report_agent,
        )

    result = final_state.get("task")
    if not isinstance(result, SleepAgentTask):
        raise RuntimeError("Task graph did not produce a SleepAgentTask.")
    return result


def _run_direct_state_graph(
    state: SleepAgentGraphState,
    *,
    analysis_service: AnalysisService | None,
    report_agent: ReportAgent | None,
) -> SleepAgentGraphState:
    nodes = _build_task_graph_nodes(
        analysis_service=analysis_service,
        report_agent=report_agent,
    )
    current_state = state
    for node_name in TASK_GRAPH_NODE_ORDER:
        if current_state.get("errors"):
            break
        current_state = {**current_state, **nodes[node_name](current_state)}
    return current_state


def _build_task_graph_nodes(
    *,
    analysis_service: AnalysisService | None,
    report_agent: ReportAgent | None,
) -> dict[str, GraphNode]:
    resolved_analysis_service = analysis_service or AnalysisService()
    resolved_report_agent = report_agent or ReportAgent()
    return {
        LOAD_RECORD_NODE: lambda state: _load_record_node(
            state,
            resolved_analysis_service,
        ),
        QUALITY_CHECK_NODE: _quality_check_node,
        SLEEP_STAGING_NODE: _sleep_staging_node,
        RESPIRATORY_DETECTION_NODE: _respiratory_detection_node,
        RISK_ASSESSMENT_NODE: _risk_assessment_node,
        MEDICAL_RAG_NODE: _medical_rag_node,
        REPORT_GENERATION_NODE: lambda state: _report_generation_node(
            state,
            resolved_report_agent,
        ),
        CARE_PLAN_NODE: _care_plan_node,
        CHAT_CONTEXT_NODE: _chat_context_node,
    }


def _initial_state(task: SleepAgentTask) -> SleepAgentGraphState:
    return {
        "task": task,
        "events": list(task.events),
        "artifacts": list(task.artifacts),
        "errors": list(task.errors),
    }


def _load_record_node(
    state: SleepAgentGraphState,
    analysis_service: AnalysisService,
) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, LOAD_RECORD_NODE)
    started = perf_counter()

    try:
        analysis_run = analysis_service.run_analysis(task.analysis_request)
        duration_ms = _elapsed_ms(started)
        tool_call = _tool_call(
            task,
            LOAD_RECORD_NODE,
            "AnalysisService.run_analysis",
            f"record_id={task.analysis_request.record_id}",
            f"record_status={analysis_run.record_status.value}",
            ToolCallStatus.SUCCESS,
            duration_ms,
        )
        task = _complete_step(task, LOAD_RECORD_NODE, [tool_call], duration_ms)
        events = _append_event(
            task,
            events,
            AgentEventType.TOOL_CALLED,
            "真实分析服务已调用",
            "AnalysisService 已完成记录解析、质量检查、分期和风险摘要。",
            step_id=LOAD_RECORD_NODE,
            payload={
                "tool_call": tool_call.model_dump(mode="json", by_alias=True),
                "record_status": analysis_run.record_status.value,
            },
        )
    except Exception as exc:
        return _fail_step(
            task,
            events,
            artifacts,
            LOAD_RECORD_NODE,
            "真实分析服务失败",
            str(exc),
        )

    events = _append_node_finding(task, events, LOAD_RECORD_NODE, analysis_run.record_result)
    if analysis_run.record_result.status == AnalysisNodeStatus.FAILED:
        return _fail_step(
            task,
            events,
            artifacts,
            LOAD_RECORD_NODE,
            "本地数据缺失",
            analysis_run.record_result.error or "LoadRecord failed.",
            analysis_run=analysis_run,
        )

    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        "本地睡眠记录已解析",
        "已获得 EDF/XML 本地路径和记录元数据。",
        step_id=LOAD_RECORD_NODE,
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=analysis_run,
    )


def _quality_check_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    return _analysis_result_node(
        state,
        QUALITY_CHECK_NODE,
        "信号质量检查完成",
        "信号质量检查发现阻断性问题",
        _analysis_run(state).quality_result,
    )


def _sleep_staging_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    return _analysis_result_node(
        state,
        SLEEP_STAGING_NODE,
        "YASA 睡眠分期完成",
        "YASA 睡眠分期失败",
        _analysis_run(state).sleep_staging_result,
    )


def _respiratory_detection_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    return _analysis_result_node(
        state,
        RESPIRATORY_DETECTION_NODE,
        "呼吸事件摘要完成",
        "呼吸事件节点失败",
        _analysis_run(state).respiratory_result,
        fail_on_warning_status=False,
    )


def _risk_assessment_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    node_result = _analysis_run(state).risk_result
    next_state = _analysis_result_node(
        state,
        RISK_ASSESSMENT_NODE,
        "风险评估完成",
        "风险评估失败",
        node_result,
    )
    if next_state.get("errors"):
        return next_state

    task = _require_task(next_state)
    events = _state_events(next_state)
    artifacts = _state_artifacts(next_state)
    risk_level = str(node_result.payload.get("risk_level", "unknown"))
    respiratory_model_status = str(
        node_result.payload.get(
            "respiratory_model_status",
            "not_validated_for_risk_conclusion",
        )
    )
    evidence_chain = node_result.payload.get("evidence_chain", [])
    if respiratory_model_status == "not_validated_for_risk_conclusion":
        evidence_chain = [
            *evidence_chain,
            (
                "respiratory_model_status=not_validated_for_risk_conclusion; "
                "呼吸模型未通过门控，不进入 Agent 主风险结论。"
            ),
        ]
    risk_artifact = _artifact(
        task,
        ArtifactType.RISK_SUMMARY,
        "风险摘要 Artifact",
        ArtifactStatus.READY,
        (
            f"风险等级：{risk_level}\n\n"
            f"呼吸模型状态：{respiratory_model_status}\n\n"
            f"AHI：{node_result.metrics.get('ahi', 'unknown')}\n\n"
            f"{MEDICAL_DISCLAIMER}"
        ),
        RISK_ASSESSMENT_NODE,
    )
    evidence_artifact = _artifact(
        task,
        ArtifactType.EVIDENCE_CHAIN,
        "证据链 Artifact",
        ArtifactStatus.READY,
        "\n".join(f"- {item}" for item in evidence_chain) + f"\n\n{MEDICAL_DISCLAIMER}",
        RISK_ASSESSMENT_NODE,
    )
    artifacts = [*artifacts, risk_artifact, evidence_artifact]
    events = _append_artifact_event(task, events, risk_artifact)
    events = _append_artifact_event(task, events, evidence_artifact)
    return _state(task, events, artifacts, analysis_run=_analysis_run(state))


def _medical_rag_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, MEDICAL_RAG_NODE)
    started = perf_counter()
    query_terms = ["ahi", "sleep_efficiency", "respiratory_events", "medical_safety"]
    chunks = retrieve_report_context(query_terms, top_k=3)
    duration_ms = _elapsed_ms(started)
    tool_call = _tool_call(
        task,
        MEDICAL_RAG_NODE,
        "retrieve_report_context",
        ", ".join(query_terms),
        f"retrieved_chunks={len(chunks)}",
        ToolCallStatus.SUCCESS,
        duration_ms,
    )
    task = _complete_step(task, MEDICAL_RAG_NODE, [tool_call], duration_ms)
    events = _append_event(
        task,
        events,
        AgentEventType.TOOL_CALLED,
        "医学知识检索已调用",
        "已检索内部种子知识；这些内容不能伪装成临床指南。",
        step_id=MEDICAL_RAG_NODE,
        payload={"tool_call": tool_call.model_dump(mode="json", by_alias=True)},
    )
    events = _append_event(
        task,
        events,
        AgentEventType.FINDING_CREATED,
        "医学知识边界已记录",
        "RAG 来源为内部 seed knowledge，用户可见内容必须保留辅助分析边界。",
        step_id=MEDICAL_RAG_NODE,
        payload={
            "retrieved_chunks": [
                chunk.model_dump(mode="json") for chunk in chunks
            ],
            "source_boundary": "internal_seed_only",
        },
    )
    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        "医学知识检索完成",
        "已准备报告生成所需的安全边界和知识片段。",
        step_id=MEDICAL_RAG_NODE,
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=_analysis_run(state),
        rag_chunks=chunks,
    )


def _report_generation_node(
    state: SleepAgentGraphState,
    report_agent: ReportAgent,
) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, REPORT_GENERATION_NODE)
    analysis = _analysis_run(state).sleep_analysis_result
    if analysis is None:
        return _fail_step(
            task,
            events,
            artifacts,
            REPORT_GENERATION_NODE,
            "报告生成缺少分析结果",
            "AnalysisRunResult did not include sleep_analysis_result.",
        )

    started = perf_counter()
    report = report_agent.run(analysis, use_deepseek_report=task.use_deepseek_report)
    duration_ms = _elapsed_ms(started)
    tool_call = _tool_call(
        task,
        REPORT_GENERATION_NODE,
        "ReportAgent.run",
        f"risk_level={analysis.risk_level.value}",
        "created elder/family/doctor/technical artifacts",
        ToolCallStatus.SUCCESS,
        duration_ms,
    )
    task = _complete_step(task, REPORT_GENERATION_NODE, [tool_call], duration_ms)
    events = _append_event(
        task,
        events,
        AgentEventType.TOOL_CALLED,
        "报告生成工具已调用",
        "ReportAgent 已生成多角色报告内容。",
        step_id=REPORT_GENERATION_NODE,
        payload={"tool_call": tool_call.model_dump(mode="json", by_alias=True)},
    )

    report_artifacts = [
        _artifact(
            task,
            ArtifactType.ELDER_REPORT,
            "老人易懂版报告 Artifact",
            ArtifactStatus.READY,
            f"{report.elder_report}\n\n{MEDICAL_DISCLAIMER}",
            REPORT_GENERATION_NODE,
        ),
        _artifact(
            task,
            ArtifactType.FAMILY_REPORT,
            "家属沟通版报告 Artifact",
            ArtifactStatus.READY,
            _family_report_content(report),
            REPORT_GENERATION_NODE,
        ),
        _artifact(
            task,
            ArtifactType.DOCTOR_REPORT,
            "医生专业版报告 Artifact",
            ArtifactStatus.DRAFT,
            f"{report.professional_report}\n\n{MEDICAL_DISCLAIMER}",
            REPORT_GENERATION_NODE,
        ),
        _artifact(
            task,
            ArtifactType.TECHNICAL_REPORT,
            "技术说明 Artifact",
            ArtifactStatus.READY,
            _technical_report_content(_analysis_run(state)),
            REPORT_GENERATION_NODE,
        ),
    ]
    artifacts = [*artifacts, *report_artifacts]
    for item in report_artifacts:
        events = _append_artifact_event(task, events, item)
    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        "多角色报告 Artifact 已创建",
        "已生成老人、家属、医生和技术说明 Artifact。",
        step_id=REPORT_GENERATION_NODE,
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=_analysis_run(state),
        report=report,
        rag_chunks=state.get("rag_chunks", []),
    )


def _care_plan_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, CARE_PLAN_NODE)
    report = state.get("report")
    if not isinstance(report, MockSleepReport):
        return _fail_step(
            task,
            events,
            artifacts,
            CARE_PLAN_NODE,
            "观察计划缺少报告上下文",
            "ReportGenerationNode did not produce a report.",
        )

    started = perf_counter()
    suggestions = report.care_suggestions or ["继续观察睡眠质量、白天嗜睡和夜间憋醒情况。"]
    duration_ms = _elapsed_ms(started)
    tool_call = _tool_call(
        task,
        CARE_PLAN_NODE,
        "CarePlanTemplate",
        "report.care_suggestions",
        f"suggestion_count={len(suggestions)}",
        ToolCallStatus.SUCCESS,
        duration_ms,
    )
    task = _complete_step(task, CARE_PLAN_NODE, [tool_call], duration_ms)
    events = _append_event(
        task,
        events,
        AgentEventType.TOOL_CALLED,
        "观察计划模板已调用",
        "已根据报告建议生成待确认的观察计划。",
        step_id=CARE_PLAN_NODE,
        payload={"tool_call": tool_call.model_dump(mode="json", by_alias=True)},
    )
    care_plan = _artifact(
        task,
        ArtifactType.CARE_PLAN,
        "关怀观察计划 Artifact",
        ArtifactStatus.DRAFT,
        _care_plan_content(suggestions),
        CARE_PLAN_NODE,
    )
    artifacts = [*artifacts, care_plan]
    events = _append_artifact_event(task, events, care_plan)
    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        "观察计划草稿已创建",
        "关怀计划需要用户确认后再启用或导出。",
        step_id=CARE_PLAN_NODE,
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=_analysis_run(state),
        report=report,
        rag_chunks=state.get("rag_chunks", []),
    )


def _chat_context_node(state: SleepAgentGraphState) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, CHAT_CONTEXT_NODE)
    started = perf_counter()
    duration_ms = _elapsed_ms(started)
    tool_call = _tool_call(
        task,
        CHAT_CONTEXT_NODE,
        "ChatContextBuilder",
        "task events, artifacts, caveats",
        "dialogue context ready",
        ToolCallStatus.SUCCESS,
        duration_ms,
    )
    task = _complete_step(task, CHAT_CONTEXT_NODE, [tool_call], duration_ms)
    events = _append_event(
        task,
        events,
        AgentEventType.TOOL_CALLED,
        "对话上下文构建器已调用",
        "已整理事件、Artifact 和 caveat，供后续问答使用。",
        step_id=CHAT_CONTEXT_NODE,
        payload={"tool_call": tool_call.model_dump(mode="json", by_alias=True)},
    )
    events = _append_event(
        task,
        events,
        AgentEventType.FINDING_CREATED,
        "对话上下文已准备",
        "后续问答必须保留模型 caveat 和医学辅助边界。",
        step_id=CHAT_CONTEXT_NODE,
        payload={"caveats": _analysis_run(state).caveats},
    )
    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        "对话上下文完成",
        "任务上下文可用于 Agent Run 恢复和后续问答。",
        step_id=CHAT_CONTEXT_NODE,
    )
    events = _append_event(
        task,
        events,
        AgentEventType.TASK_COMPLETED,
        "任务已完成",
        "SleepAgent 已完成真实分析任务状态图。",
        payload={"artifact_count": len(artifacts)},
    )
    task = task.model_copy(
        update={
            "status": TaskStatus.COMPLETED,
            "events": events,
            "artifacts": artifacts,
            "next_actions": _completed_next_actions(),
            "analysis_result": _analysis_run(state).sleep_analysis_result,
            "report_result": state.get("report"),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=_analysis_run(state),
        report=state.get("report"),
        rag_chunks=state.get("rag_chunks", []),
    )


def _analysis_result_node(
    state: SleepAgentGraphState,
    step_id: str,
    success_title: str,
    failure_title: str,
    node_result: AnalysisNodeResult,
    *,
    fail_on_warning_status: bool = False,
) -> SleepAgentGraphState:
    task, events, artifacts = _prepare_step(state, step_id)
    started = perf_counter()
    duration_ms = _elapsed_ms(started)
    tool_call = _tool_call(
        task,
        step_id,
        node_result.name,
        "AnalysisRunResult node payload",
        f"status={node_result.status.value}",
        ToolCallStatus.SUCCESS,
        duration_ms,
    )
    task = _complete_step(task, step_id, [tool_call], duration_ms)
    events = _append_event(
        task,
        events,
        AgentEventType.TOOL_CALLED,
        f"{node_result.name} 结果已读取",
        "任务图已读取真实分析服务产生的结构化节点结果。",
        step_id=step_id,
        payload={"tool_call": tool_call.model_dump(mode="json", by_alias=True)},
    )
    events = _append_node_finding(task, events, step_id, node_result)

    failed = node_result.status == AnalysisNodeStatus.FAILED
    warning_failure = (
        fail_on_warning_status
        and node_result.status == AnalysisNodeStatus.SKIPPED_WITH_WARNING
    )
    if failed or warning_failure:
        return _fail_step(
            task,
            events,
            artifacts,
            step_id,
            failure_title,
            node_result.error or "; ".join(node_result.warnings) or failure_title,
            analysis_run=_analysis_run(state),
        )

    events = _append_event(
        task,
        events,
        AgentEventType.STEP_COMPLETED,
        success_title,
        _node_success_message(node_result),
        step_id=step_id,
    )
    return _state(
        task,
        events,
        artifacts,
        analysis_run=_analysis_run(state),
        report=state.get("report"),
        rag_chunks=state.get("rag_chunks", []),
    )


def _prepare_step(
    state: SleepAgentGraphState,
    step_id: str,
) -> tuple[SleepAgentTask, list[AgentEvent], list[Artifact]]:
    task = _require_task(state)
    events = _state_events(state)
    artifacts = _state_artifacts(state)
    task = _set_step_status(task, step_id, AgentPlanStepStatus.RUNNING)
    events = _append_event(
        task,
        events,
        AgentEventType.STEP_STARTED,
        _step_title(task, step_id),
        f"开始执行 {step_id} 节点。",
        step_id=step_id,
    )
    task = task.model_copy(update={"events": events, "updated_at": datetime.now(timezone.utc)})
    return task, events, artifacts


def _complete_step(
    task: SleepAgentTask,
    step_id: str,
    tool_calls: list[ToolCall],
    duration_ms: int,
) -> SleepAgentTask:
    updated_plan: list[AgentPlanStep] = []
    for step in task.plan:
        if step.id != step_id:
            updated_plan.append(step)
            continue
        updated_plan.append(
            step.model_copy(
                update={
                    "status": AgentPlanStepStatus.COMPLETED,
                    "tool_calls": [*step.tool_calls, *tool_calls],
                    "duration_ms": duration_ms,
                }
            )
        )
    return task.model_copy(
        update={
            "plan": updated_plan,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def _fail_step(
    task: SleepAgentTask,
    events: list[AgentEvent],
    artifacts: list[Artifact],
    step_id: str,
    title: str,
    message: str,
    **state_values: Any,
) -> SleepAgentGraphState:
    events = _append_event(
        task,
        events,
        AgentEventType.ERROR,
        title,
        message,
        step_id=step_id,
        payload={"error_code": _error_code(step_id), "step_id": step_id},
    )
    task = task.model_copy(
        update={
            "status": TaskStatus.FAILED,
            "events": events,
            "artifacts": artifacts,
            "errors": [*task.errors, message],
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return _state(
        task,
        events,
        artifacts,
        errors=[message],
        **state_values,
    )


def _append_node_finding(
    task: SleepAgentTask,
    events: list[AgentEvent],
    step_id: str,
    node_result: AnalysisNodeResult,
) -> list[AgentEvent]:
    return _append_event(
        task,
        events,
        AgentEventType.FINDING_CREATED,
        f"{node_result.name} 发现",
        _node_success_message(node_result),
        step_id=step_id,
        payload={
            "status": node_result.status.value,
            "warnings": node_result.warnings,
            "caveats": node_result.caveats,
            "metrics": node_result.metrics,
            "source_paths": node_result.source_paths,
            "payload": node_result.payload,
        },
    )


def _append_artifact_event(
    task: SleepAgentTask,
    events: list[AgentEvent],
    artifact: Artifact,
) -> list[AgentEvent]:
    return _append_event(
        task,
        events,
        AgentEventType.ARTIFACT_CREATED,
        artifact.title,
        f"已创建 {artifact.type.value} Artifact。",
        step_id=artifact.created_by_step_id,
        payload={"artifact": artifact.model_dump(mode="json", by_alias=True)},
    )


def _append_event(
    task: SleepAgentTask,
    events: list[AgentEvent],
    event_type: AgentEventType,
    title: str,
    message: str,
    *,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> list[AgentEvent]:
    event = AgentEvent(
        id=f"{task.id}-event-{len(events) + 1:04d}",
        type=event_type,
        step_id=step_id,
        title=title,
        message=message,
        payload=payload,
    )
    return [*events, event]


def _set_step_status(
    task: SleepAgentTask,
    step_id: str,
    status: AgentPlanStepStatus,
) -> SleepAgentTask:
    return task.model_copy(
        update={
            "plan": [
                step.model_copy(update={"status": status})
                if step.id == step_id
                else step
                for step in task.plan
            ],
            "updated_at": datetime.now(timezone.utc),
        }
    )


def _state(
    task: SleepAgentTask,
    events: list[AgentEvent],
    artifacts: list[Artifact],
    **values: Any,
) -> SleepAgentGraphState:
    updated_task = task.model_copy(
        update={
            "events": events,
            "artifacts": artifacts,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    state: SleepAgentGraphState = {
        "task": updated_task,
        "events": events,
        "artifacts": artifacts,
    }
    state.update(values)
    return state


def _analysis_run(state: SleepAgentGraphState) -> Any:
    analysis_run = state.get("analysis_run")
    if analysis_run is None:
        raise RuntimeError("Task graph state is missing analysis_run.")
    return analysis_run


def _require_task(state: SleepAgentGraphState) -> SleepAgentTask:
    task = state.get("task")
    if not isinstance(task, SleepAgentTask):
        raise RuntimeError("Task graph state is missing task.")
    return task


def _state_events(state: SleepAgentGraphState) -> list[AgentEvent]:
    return list(state.get("events", []))


def _state_artifacts(state: SleepAgentGraphState) -> list[Artifact]:
    return list(state.get("artifacts", []))


def _tool_call(
    task: SleepAgentTask,
    step_id: str,
    tool_name: str,
    input_summary: str,
    output_summary: str,
    status: ToolCallStatus,
    duration_ms: int,
) -> ToolCall:
    return ToolCall(
        id=f"{task.id}-{step_id}-tool-001",
        tool_name=tool_name,
        input_summary=input_summary,
        output_summary=output_summary,
        status=status,
        duration_ms=duration_ms,
    )


def _artifact(
    task: SleepAgentTask,
    artifact_type: ArtifactType,
    title: str,
    status: ArtifactStatus,
    content: str,
    step_id: str,
) -> Artifact:
    return Artifact(
        id=f"{task.id}-{artifact_type.value}",
        type=artifact_type,
        title=title,
        status=status,
        content=content,
        created_by_step_id=step_id,
    )


def _family_report_content(report: MockSleepReport) -> str:
    suggestions = "\n".join(f"- {item}" for item in report.care_suggestions)
    return (
        f"{report.elder_report}\n\n"
        "家属重点关注：\n"
        f"{suggestions}\n\n"
        f"{MEDICAL_DISCLAIMER}"
    )


def _technical_report_content(analysis_run: Any) -> str:
    return (
        "技术说明：\n"
        f"- record_status: {analysis_run.record_status.value}\n"
        f"- quality_status: {analysis_run.quality_result.status.value}\n"
        f"- sleep_staging_status: {analysis_run.sleep_staging_result.status.value}\n"
        f"- respiratory_status: {analysis_run.respiratory_result.status.value}\n"
        f"- risk_status: {analysis_run.risk_result.status.value}\n"
        f"- caveats: {', '.join(analysis_run.caveats)}\n\n"
        f"{MEDICAL_DISCLAIMER}"
    )


def _care_plan_content(suggestions: list[str]) -> str:
    body = "\n".join(f"- {item}" for item in suggestions)
    return (
        "观察计划草稿：\n"
        f"{body}\n\n"
        "启用前需要用户或家属确认，不会自动发送真实通知。\n\n"
        f"{MEDICAL_DISCLAIMER}"
    )


def _completed_next_actions() -> list[NextAction]:
    return [
        NextAction(
            id="review-doctor-report",
            label="确认医生摘要",
            description="医生专业版 Artifact 需要人工确认后再导出。",
            action_type=NextActionType.CONFIRM,
            target=ArtifactType.DOCTOR_REPORT.value,
            requires_confirmation=True,
        ),
        NextAction(
            id="confirm-care-plan",
            label="确认观察计划",
            description="关怀观察计划需要确认后才可启用。",
            action_type=NextActionType.CARE_PLAN,
            target=ArtifactType.CARE_PLAN.value,
            requires_confirmation=True,
        ),
        NextAction(
            id="ask-followup",
            label="继续问答",
            description="基于本次任务上下文继续解释报告。",
            action_type=NextActionType.CHAT,
            target="chat",
        ),
    ]


def _step_title(task: SleepAgentTask, step_id: str) -> str:
    for step in task.plan:
        if step.id == step_id:
            return step.title
    return step_id


def _node_success_message(node_result: AnalysisNodeResult) -> str:
    warning_suffix = (
        f" warnings={len(node_result.warnings)}" if node_result.warnings else ""
    )
    caveat_suffix = f" caveats={len(node_result.caveats)}" if node_result.caveats else ""
    return f"{node_result.name} status={node_result.status.value}.{warning_suffix}{caveat_suffix}"


def _error_code(step_id: str) -> str:
    if step_id == LOAD_RECORD_NODE:
        return "missing_local_data"
    if step_id == SLEEP_STAGING_NODE:
        return "sleep_staging_failed"
    if step_id == QUALITY_CHECK_NODE:
        return "quality_check_failed"
    return "task_graph_failed"


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
