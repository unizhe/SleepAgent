import time

import httpx
import streamlit as st

from api_client import (
    DEFAULT_API_BASE_URL,
    build_agent_run_steps,
    build_agent_timeline,
    build_export_markdown,
    build_family_share_summary,
    build_historical_trend_rows,
    build_key_evidence_rows,
    build_next_action_groups,
    build_patient_snapshot,
    build_risk_evidence_chain,
    build_role_report_sections,
    extract_agent_orchestration_summary,
    extract_dashboard_summary,
    extract_report_sections,
    extract_respiratory_event_rows,
    extract_respiratory_trend_rows,
    extract_sleep_stage_rows,
    fetch_agent_orchestration,
    fetch_mock_analysis,
    fetch_mock_report,
    fetch_mock_report_llm,
    format_risk_badge,
    format_risk_level,
)


RECOMMENDED_QUESTIONS = [
    "为什么系统判断为中等风险？",
    "AHI 是什么？",
    "这个结果严重吗？",
    "我需要去医院吗？",
    "这份报告怎么给医生看？",
    "最近一周趋势怎么样？",
]

REPORT_CARD_ORDER = ["老人易懂版", "家属照护版", "医生专业版", "技术说明版"]


def render_risk_callout(summary: dict) -> None:
    message = (
        f"今晚睡眠呼吸风险：{format_risk_level(summary['risk_level'])}。"
        f"系统检测到 AHI 约 {summary['ahi']:.1f}，"
        f"疑似呼吸暂停 {summary['suspected_apnea_count']} 次，"
        f"低通气 {summary['hypopnea_count']} 次。"
        "建议结合连续多晚数据、白天症状和医生意见进一步判断。"
    )
    if summary["risk_level"] == "high":
        st.error(message)
    elif summary["risk_level"] == "moderate":
        st.warning(message)
    else:
        st.success(message)


def format_spo2(value: object) -> str:
    if value is None:
        return "暂无"
    return f"{float(value):.1f}%"


def rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    headers = list(rows[0])
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(header, "")) for header in headers))
    return "\n".join(lines)


def init_session_state() -> None:
    defaults = {
        "analysis_status": "idle",
        "payload": None,
        "report_payload": None,
        "agent_payload": None,
        "analysis_params": None,
        "followup_question": RECOMMENDED_QUESTIONS[0],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def reset_analysis_state() -> None:
    st.session_state.analysis_status = "idle"
    st.session_state.payload = None
    st.session_state.report_payload = None
    st.session_state.agent_payload = None
    st.session_state.analysis_params = None
    st.cache_data.clear()


def build_analysis_params(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_rate: float,
    use_deepseek_report: bool,
    use_langgraph_agent: bool,
) -> dict:
    return {
        "api_base_url": api_base_url,
        "record_id": record_id,
        "subject_id": subject_id,
        "duration_hours": duration_hours,
        "seed": int(seed),
        "abnormal_event_rate_per_hour": abnormal_rate,
        "use_deepseek_report": use_deepseek_report,
        "use_langgraph_agent": use_langgraph_agent,
    }


@st.cache_data(ttl=30, show_spinner=False)
def load_mock_analysis(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
) -> dict:
    return fetch_mock_analysis(
        api_base_url=api_base_url,
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_mock_report(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    use_deepseek_report: bool,
) -> dict:
    if use_deepseek_report:
        return fetch_mock_report_llm(
            api_base_url=api_base_url,
            record_id=record_id,
            subject_id=subject_id,
            duration_hours=duration_hours,
            seed=seed,
            abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
            use_deepseek=True,
        )
    return fetch_mock_report(
        api_base_url=api_base_url,
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_agent_orchestration(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    user_question: str,
    history_summary: str,
    use_deepseek_report: bool,
    use_langgraph_agent: bool,
) -> dict:
    cleaned_question = user_question.strip() or None
    cleaned_history = history_summary.strip()
    dialogue_context = (
        {"history_summary": cleaned_history}
        if cleaned_history
        else None
    )
    return fetch_agent_orchestration(
        api_base_url=api_base_url,
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
        user_question=cleaned_question,
        dialogue_context=dialogue_context,
        use_deepseek_report=use_deepseek_report,
        use_langgraph=use_langgraph_agent,
    )


def fetch_analysis_bundle(params: dict, question: str, history_summary: str) -> tuple[dict, dict, dict]:
    payload = load_mock_analysis(
        api_base_url=params["api_base_url"],
        record_id=params["record_id"],
        subject_id=params["subject_id"],
        duration_hours=params["duration_hours"],
        seed=params["seed"],
        abnormal_event_rate_per_hour=params["abnormal_event_rate_per_hour"],
    )
    report_payload = load_mock_report(
        api_base_url=params["api_base_url"],
        record_id=params["record_id"],
        subject_id=params["subject_id"],
        duration_hours=params["duration_hours"],
        seed=params["seed"],
        abnormal_event_rate_per_hour=params["abnormal_event_rate_per_hour"],
        use_deepseek_report=params["use_deepseek_report"],
    )
    agent_payload = load_agent_orchestration(
        api_base_url=params["api_base_url"],
        record_id=params["record_id"],
        subject_id=params["subject_id"],
        duration_hours=params["duration_hours"],
        seed=params["seed"],
        abnormal_event_rate_per_hour=params["abnormal_event_rate_per_hour"],
        user_question=question,
        history_summary=history_summary,
        use_deepseek_report=params["use_deepseek_report"],
        use_langgraph_agent=params["use_langgraph_agent"],
    )
    return payload, report_payload, agent_payload


def render_idle_workspace(selected_role: str) -> None:
    st.subheader("Agent 驱动的睡眠健康分析工作台")
    intro_cols = st.columns([2, 1])
    with intro_cols[0]:
        st.markdown(
            "点击开始后，SleepAgent 会依次执行数据读取、睡眠分期、呼吸事件检测、"
            "医学知识检索、报告生成和对话建议准备。"
        )
        st.markdown("分析完成前，页面不会直接展开完整报告、图表或原始数据。")
    with intro_cols[1]:
        st.metric("当前任务状态", "待分析")
        st.metric("当前视角", selected_role)

    st.markdown("**本次 Agent Run 将执行**")
    columns = st.columns(3)
    planned_steps = [
        ("数据读取", "读取 PSG 元数据和本次记录摘要。"),
        ("睡眠分期", "识别 Wake / REM / NREM 并计算睡眠效率。"),
        ("呼吸事件检测", "识别低通气和疑似呼吸暂停事件。"),
        ("医学知识检索", "检索 AHI、OSA 风险和安全边界。"),
        ("报告生成", "生成老人、家属、医生和技术版报告。"),
        ("对话建议", "准备可追问问题和本次记录上下文。"),
    ]
    for column, (title, body) in zip(columns * 2, planned_steps, strict=True):
        with column:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(body)


def render_agent_run_console(run_steps: list[dict], *, animate: bool) -> None:
    st.subheader("Agent Run Console")
    if animate:
        progress = st.progress(0, text="SleepAgent 正在启动任务")
        status_placeholder = st.empty()
        for index, step in enumerate(run_steps, start=1):
            progress.progress(
                index / len(run_steps),
                text=f"{step['agent']} · {step['status']}",
            )
            with status_placeholder.container():
                render_agent_run_steps(run_steps[:index])
            time.sleep(0.2)
        progress.progress(1.0, text="Agent Run 已完成")
    else:
        render_agent_run_steps(run_steps)


def render_agent_run_steps(run_steps: list[dict]) -> None:
    for step in run_steps:
        with st.container(border=True):
            top_cols = st.columns([1, 2, 1, 2])
            top_cols[0].markdown(f"**{step['step']}**")
            top_cols[1].markdown(f"**{step['agent']}**")
            top_cols[2].caption(step["status"])
            top_cols[3].caption(f"{step['elapsed_ms']} ms")
            st.markdown(f"**调用工具**：{', '.join(step['tool_calls'])}")
            st.markdown(f"**输入摘要**：{step['input_summary']}")
            st.markdown(f"**输出发现**：{step['output_finding']}")


def build_frontend_context(payload: dict, report_payload: dict, agent_payload: dict) -> dict:
    summary = extract_dashboard_summary(payload)
    report_sections = extract_report_sections(report_payload)
    agent_timeline = build_agent_timeline(agent_payload)
    return {
        "summary": summary,
        "trend_rows": extract_respiratory_trend_rows(payload),
        "sleep_stage_rows": extract_sleep_stage_rows(payload),
        "event_rows": extract_respiratory_event_rows(payload),
        "report_sections": report_sections,
        "agent_summary": extract_agent_orchestration_summary(agent_payload),
        "agent_timeline": agent_timeline,
        "run_steps": build_agent_run_steps(agent_payload),
        "patient_snapshot": build_patient_snapshot(summary),
        "evidence_rows": build_key_evidence_rows(summary),
        "evidence_chain": build_risk_evidence_chain(summary),
        "action_groups": build_next_action_groups(summary),
        "role_sections": build_role_report_sections(summary, report_sections),
        "history_rows": build_historical_trend_rows(summary),
        "family_share_summary": build_family_share_summary(summary),
        "export_markdown": build_export_markdown(summary, report_sections, agent_timeline),
    }


def render_patient_sidebar(patient_snapshot: dict | None, status: str) -> None:
    st.subheader("用户信息")
    if patient_snapshot is None:
        st.markdown("**用户：** 张阿姨")
        st.markdown("**年龄：** 待分析")
        st.markdown("**记录日期：** 待分析")
        st.markdown("**数据来源：** SHHS / PSG 演示")
        st.markdown("**记录时长：** 待分析")
        st.markdown(f"**分析状态：** {status}")
        return

    st.markdown(f"**用户：** {patient_snapshot['name']}")
    st.markdown(f"**年龄：** {patient_snapshot['age']}")
    st.markdown(f"**记录日期：** {patient_snapshot['record_date']}")
    st.markdown(f"**数据来源：** {patient_snapshot['source']}")
    st.markdown(f"**记录时长：** {patient_snapshot['duration']}")
    st.markdown(f"**分析状态：** {patient_snapshot['status']}")


def render_quick_actions(context: dict | None) -> None:
    st.subheader("快捷操作")
    if context is None:
        st.caption("开始分析并生成报告后可导出、分享或生成医生摘要。")
        return

    summary = context["summary"]
    st.download_button(
        "导出报告",
        data=context["export_markdown"],
        file_name=f"{summary['record_id']}_sleepagent_report.md",
        mime="text/markdown",
        width="stretch",
    )
    st.download_button(
        "生成医生版摘要",
        data=context["role_sections"]["医生专业版"]["summary"],
        file_name=f"{summary['record_id']}_doctor_summary.txt",
        mime="text/plain",
        width="stretch",
    )
    st.download_button(
        "发送给家属",
        data=context["family_share_summary"],
        file_name=f"{summary['record_id']}_family_summary.txt",
        mime="text/plain",
        width="stretch",
    )


def render_result_workbench(context: dict, agent_payload: dict, selected_role: str) -> None:
    summary = context["summary"]
    evidence_chain = context["evidence_chain"]
    role_sections = context["role_sections"]
    trend_rows = context["trend_rows"]
    sleep_stage_rows = context["sleep_stage_rows"]
    event_rows = context["event_rows"]

    status_cols = st.columns(4)
    status_cols[0].metric("当前分析状态", "分析完成")
    status_cols[1].metric("数据质量", "良好")
    status_cols[2].metric("报告状态", "已生成")
    status_cols[3].metric("当前视角", selected_role)

    st.subheader("今日风险结论")
    render_risk_callout(summary)

    first_screen_cols = st.columns([1, 1, 1])
    with first_screen_cols[0]:
        with st.container(border=True):
            st.markdown("**风险结论**")
            st.metric("今晚睡眠呼吸风险", format_risk_level(summary["risk_level"]))
            st.caption(format_risk_badge(summary["risk_level"]))
    with first_screen_cols[1]:
        with st.container(border=True):
            st.markdown("**主要原因**")
            st.markdown(evidence_chain["primary_reason"])
    with first_screen_cols[2]:
        with st.container(border=True):
            st.markdown("**下一步行动**")
            st.markdown(evidence_chain["next_action"])

    metric_cols = st.columns(5)
    metric_cols[0].metric("AHI", f"{summary['ahi']:.2f}")
    metric_cols[1].metric("睡眠效率", f"{summary['sleep_efficiency_percent']:.1f}%")
    metric_cols[2].metric("总睡眠", f"{summary['total_sleep_minutes']:.0f} min")
    metric_cols[3].metric("疑似暂停", f"{summary['suspected_apnea_count']} 次")
    metric_cols[4].metric("最低 SpO2", format_spo2(summary.get("min_spo2_percent")))

    st.subheader("证据链解释")
    for index, reason in enumerate(evidence_chain["reason_chain"], start=1):
        st.markdown(f"{index}. {reason}")

    render_agent_run_console(context["run_steps"], animate=False)

    st.subheader("结构化报告卡片")
    selected_section = role_sections[selected_role]
    with st.container(border=True):
        st.markdown(f"**当前视角：{selected_role}**")
        st.markdown(selected_section["summary"])
        st.markdown("**建议行动**")
        for action in selected_section["actions"]:
            st.markdown(f"- {action}")

    report_cols = st.columns(2)
    for index, role_name in enumerate(REPORT_CARD_ORDER):
        section = role_sections[role_name]
        with report_cols[index % 2]:
            with st.container(border=True):
                st.markdown(f"**{role_name}**")
                st.markdown(section["summary"])
                st.markdown("**重点**")
                for item in section["focus"]:
                    st.markdown(f"- {item}")

    with st.expander("关键证据表", expanded=False):
        st.dataframe(context["evidence_rows"], width="stretch", hide_index=True)

    with st.expander("呼吸事件与血氧趋势", expanded=False):
        trend_col, event_col = st.columns([2, 1])
        with trend_col:
            if trend_rows:
                st.line_chart(trend_rows, x="minute", y="breaths_per_minute")
                st.line_chart(trend_rows, x="minute", y="spo2_percent")
            else:
                st.warning("No respiratory trend points were returned.")
        with event_col:
            st.markdown("**异常事件标记**")
            if event_rows:
                st.dataframe(event_rows, width="stretch", hide_index=True)
            else:
                st.info("本次未返回疑似低通气或呼吸暂停事件。")

    with st.expander("睡眠分期图与历史趋势", expanded=False):
        stage_col, history_col = st.columns(2)
        with stage_col:
            if sleep_stage_rows:
                st.line_chart(sleep_stage_rows, x="minute", y="stage_score")
                st.caption("纵轴：0=NREM，1=REM，2=清醒。")
            else:
                st.info("本次记录未返回睡眠分期窗口。")
        with history_col:
            st.line_chart(context["history_rows"], x="night", y="ahi")
            st.line_chart(context["history_rows"], x="night", y="sleep_efficiency_percent")
            st.caption("历史趋势入口已接入前端视图；接入真实历史库后可替换当前演示序列。")

    render_dialogue_panel(context, agent_payload)
    render_export_panel(context, trend_rows)
    render_developer_details(context, agent_payload)
    st.caption(context["report_sections"]["medical_disclaimer"])


def render_dialogue_panel(context: dict, agent_payload: dict) -> None:
    st.subheader("对话问答 Agent")
    st.markdown("**可追问问题**")
    question_cols = st.columns(3)
    for index, question in enumerate(RECOMMENDED_QUESTIONS):
        if question_cols[index % 3].button(question, key=f"quick_question_{index}"):
            st.session_state.followup_question = question

    st.text_input(
        "围绕本次记录继续追问",
        key="followup_question",
    )
    history_summary = st.text_area(
        "补充近期症状或历史摘要",
        value="",
        placeholder="例如：最近一周打鼾更明显，白天容易犯困。",
    )
    if st.button("提交追问", type="primary"):
        params = st.session_state.analysis_params
        try:
            st.session_state.agent_payload = load_agent_orchestration(
                api_base_url=params["api_base_url"],
                record_id=params["record_id"],
                subject_id=params["subject_id"],
                duration_hours=params["duration_hours"],
                seed=params["seed"],
                abnormal_event_rate_per_hour=params["abnormal_event_rate_per_hour"],
                user_question=st.session_state.followup_question,
                history_summary=history_summary,
                use_deepseek_report=params["use_deepseek_report"],
                use_langgraph_agent=params["use_langgraph_agent"],
            )
            st.rerun()
        except httpx.HTTPStatusError as exc:
            st.error(f"Backend returned HTTP {exc.response.status_code}.")
        except httpx.RequestError:
            st.error("Cannot reach the SleepAgent backend. Start FastAPI and try again.")

    agent_summary = context["agent_summary"]
    if agent_payload.get("dialogue"):
        st.markdown(agent_payload["dialogue"]["assistant_response"])
        if agent_summary["dialogue_safety_flags"]:
            st.warning("；".join(agent_summary["dialogue_safety_flags"]))
    else:
        st.info("输入问题后，SleepAgent 会结合本次记录生成回答。")


def render_export_panel(context: dict, trend_rows: list[dict]) -> None:
    summary = context["summary"]
    st.subheader("报告导出与分享")
    export_cols = st.columns(3)
    export_cols[0].download_button(
        "下载老人易懂版报告",
        data=context["role_sections"]["老人易懂版"]["summary"],
        file_name=f"{summary['record_id']}_elder_report.txt",
        mime="text/plain",
        width="stretch",
    )
    export_cols[1].download_button(
        "下载医生专业版报告",
        data=context["role_sections"]["医生专业版"]["summary"],
        file_name=f"{summary['record_id']}_professional_report.txt",
        mime="text/plain",
        width="stretch",
    )
    export_cols[2].download_button(
        "下载原始趋势 CSV",
        data=rows_to_csv(trend_rows),
        file_name=f"{summary['record_id']}_respiratory_trend.csv",
        mime="text/csv",
        width="stretch",
    )

    with st.expander("给家属的摘要"):
        st.code(context["family_share_summary"], language="text")

    with st.expander("报警推送与主动关怀入口"):
        st.markdown("- AHI 连续多晚升高：提醒家属关注。")
        st.markdown("- 最低血氧低于阈值：触发高风险提醒。")
        st.markdown("- 连续多天睡眠效率下降：生成主动关怀建议。")


def render_developer_details(context: dict, agent_payload: dict) -> None:
    with st.expander("查看原始数据与开发者详情", expanded=False):
        agent_summary = context["agent_summary"]
        dev_cols = st.columns(4)
        dev_cols[0].metric("Mode", str(agent_summary["orchestration_mode"]).title())
        dev_cols[1].metric("Steps", len(agent_summary["step_names"]))
        dev_cols[2].metric("Dialogue", "Yes" if agent_summary["dialogue_present"] else "No")
        dev_cols[3].metric(
            "Context",
            "Used" if agent_summary["dialogue_context_used"] else "Not Used",
        )
        st.markdown("**呼吸趋势原始表**")
        st.dataframe(context["trend_rows"], width="stretch", hide_index=True)
        st.markdown("**Raw Mock Payload**")
        raw_analysis, raw_report, raw_agent = st.tabs(["Analysis", "Report", "Agent"])
        with raw_analysis:
            st.json(st.session_state.payload)
        with raw_report:
            st.json(st.session_state.report_payload)
        with raw_agent:
            st.json(agent_payload)


def run_analysis(params: dict) -> None:
    st.session_state.analysis_status = "running"
    try:
        with st.spinner("SleepAgent 正在执行 Agent Run"):
            payload, report_payload, agent_payload = fetch_analysis_bundle(
                params,
                question=st.session_state.followup_question,
                history_summary="",
            )
    except httpx.HTTPStatusError as exc:
        st.session_state.analysis_status = "idle"
        st.error(f"Backend returned HTTP {exc.response.status_code}.")
        st.stop()
    except httpx.RequestError:
        st.session_state.analysis_status = "idle"
        st.error("Cannot reach the SleepAgent backend. Start FastAPI and try again.")
        st.code("uvicorn backend.main:app --reload", language="bash")
        st.stop()

    context = build_frontend_context(payload, report_payload, agent_payload)
    render_agent_run_console(context["run_steps"], animate=True)
    st.session_state.payload = payload
    st.session_state.report_payload = report_payload
    st.session_state.agent_payload = agent_payload
    st.session_state.analysis_params = params
    st.session_state.analysis_status = "completed"


st.set_page_config(
    page_title="SleepAgent 智能睡眠健康分析工作台",
    layout="wide",
)
init_session_state()

st.title("SleepAgent 智能睡眠健康分析工作台")
st.caption("Agent 驱动的睡眠呼吸风险分析、解释、报告和追问工作台")

with st.sidebar:
    st.subheader("用户视角")
    selected_role = st.radio(
        "选择本次报告视角",
        ["老人易懂版", "家属照护版", "医生专业版"],
        label_visibility="collapsed",
    )

with st.sidebar.expander("开发者模式 / Debug Panel", expanded=False):
    st.markdown("**Mock Request**")
    api_base_url = st.text_input("API Base URL", value=DEFAULT_API_BASE_URL)
    record_id = st.text_input("Record ID", value="mock-shhs-0001")
    subject_id = st.text_input("Subject ID", value="mock-subject-0001")
    duration_hours = st.slider(
        "Duration Hours",
        min_value=0.5,
        max_value=12.0,
        value=8.0,
        step=0.5,
    )
    seed = st.number_input("Seed", min_value=0, max_value=1_000_000, value=42, step=1)
    abnormal_rate = st.slider(
        "Abnormal Events / Hour",
        min_value=0.0,
        max_value=60.0,
        value=6.0,
        step=0.5,
    )
    use_deepseek_report = st.checkbox("DeepSeek report", value=False)
    use_langgraph_agent = st.checkbox("LangGraph agent", value=False)

    if st.button("刷新分析缓存"):
        reset_analysis_state()

params = build_analysis_params(
    api_base_url=api_base_url,
    record_id=record_id,
    subject_id=subject_id,
    duration_hours=duration_hours,
    seed=int(seed),
    abnormal_rate=abnormal_rate,
    use_deepseek_report=use_deepseek_report,
    use_langgraph_agent=use_langgraph_agent,
)

active_context = None
if st.session_state.analysis_status == "completed":
    active_context = build_frontend_context(
        st.session_state.payload,
        st.session_state.report_payload,
        st.session_state.agent_payload,
    )

with st.sidebar:
    render_patient_sidebar(
        active_context["patient_snapshot"] if active_context else None,
        "分析完成" if active_context else "待分析",
    )
    render_quick_actions(active_context)

control_cols = st.columns([1, 1, 2])
start_clicked = control_cols[0].button(
    "开始分析",
    type="primary",
    disabled=st.session_state.analysis_status == "running",
)
reset_clicked = control_cols[1].button("重新分析")
control_cols[2].caption("点击开始后，完整报告和图表会在 Agent Run 完成后出现。")

if reset_clicked:
    reset_analysis_state()
    st.rerun()

if start_clicked:
    run_analysis(params)
    active_context = build_frontend_context(
        st.session_state.payload,
        st.session_state.report_payload,
        st.session_state.agent_payload,
    )

if active_context is None:
    render_idle_workspace(selected_role)
else:
    render_result_workbench(
        active_context,
        st.session_state.agent_payload,
        selected_role,
    )
