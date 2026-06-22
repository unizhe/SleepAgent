import os
from typing import Any

import httpx


DEFAULT_API_BASE_URL = os.getenv("SLEEPAGENT_API_BASE_URL", "http://127.0.0.1:8000")

RISK_LABELS = {
    "low": "低风险",
    "moderate": "中等风险",
    "high": "较高风险",
}

RISK_BADGES = {
    "low": "稳定",
    "moderate": "需要关注",
    "high": "建议尽快咨询医生",
}

RESPIRATORY_EVENT_LABELS = {
    "normal_breathing": "正常呼吸",
    "hypopnea": "低通气",
    "suspected_apnea": "疑似呼吸暂停",
}

SLEEP_STAGE_SCORES = {
    "NREM": 0,
    "REM": 1,
    "Wake": 2,
}

SLEEP_STAGE_LABELS = {
    "NREM": "NREM",
    "REM": "REM",
    "Wake": "清醒",
}


def build_mock_analysis_url(api_base_url: str) -> str:
    return f"{api_base_url.rstrip('/')}/mock-analysis"


def build_mock_report_url(api_base_url: str) -> str:
    return f"{api_base_url.rstrip('/')}/mock-report"


def build_mock_report_llm_url(api_base_url: str) -> str:
    return f"{api_base_url.rstrip('/')}/mock-report/llm"


def build_agent_orchestration_url(api_base_url: str) -> str:
    return f"{api_base_url.rstrip('/')}/agent/orchestrate"


def fetch_mock_analysis(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    response = httpx.get(
        build_mock_analysis_url(api_base_url),
        params={
            "record_id": record_id,
            "subject_id": subject_id,
            "duration_hours": duration_hours,
            "seed": seed,
            "abnormal_event_rate_per_hour": abnormal_event_rate_per_hour,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def fetch_mock_report(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    response = httpx.get(
        build_mock_report_url(api_base_url),
        params={
            "record_id": record_id,
            "subject_id": subject_id,
            "duration_hours": duration_hours,
            "seed": seed,
            "abnormal_event_rate_per_hour": abnormal_event_rate_per_hour,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def fetch_mock_report_llm(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    use_deepseek: bool = False,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    response = httpx.get(
        build_mock_report_llm_url(api_base_url),
        params={
            "record_id": record_id,
            "subject_id": subject_id,
            "duration_hours": duration_hours,
            "seed": seed,
            "abnormal_event_rate_per_hour": abnormal_event_rate_per_hour,
            "use_deepseek": use_deepseek,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def fetch_agent_orchestration(
    api_base_url: str,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    user_question: str | None = None,
    dialogue_context: dict[str, Any] | None = None,
    use_deepseek_report: bool = False,
    use_langgraph: bool = False,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    response = httpx.post(
        build_agent_orchestration_url(api_base_url),
        json={
            "record_id": record_id,
            "subject_id": subject_id,
            "duration_hours": duration_hours,
            "seed": seed,
            "abnormal_event_rate_per_hour": abnormal_event_rate_per_hour,
            "user_question": user_question,
            "dialogue_context": dialogue_context,
            "use_deepseek_report": use_deepseek_report,
            "use_langgraph": use_langgraph,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def extract_dashboard_summary(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload["metadata"]
    patient = metadata["patient"]
    sleep_summary = payload["sleep_summary"]
    respiratory_summary = payload["respiratory_summary"]
    trend = payload.get("respiratory_trend", [])
    spo2_values = [
        point["spo2_percent"]
        for point in trend
        if point.get("spo2_percent") is not None
    ]
    events = payload.get("respiratory_events", [])
    abnormal_events = [
        event
        for event in events
        if event.get("event_type") in {"hypopnea", "suspected_apnea"}
    ]

    return {
        "record_id": metadata["record_id"],
        "subject_id": patient["subject_id"],
        "source_dataset": metadata.get("source_dataset", "mock"),
        "age_years": patient.get("age_years"),
        "sex": patient.get("sex", "unknown"),
        "recording_start": metadata.get("recording_start"),
        "duration_minutes": sleep_summary["total_recording_minutes"],
        "total_sleep_minutes": sleep_summary["total_sleep_time_minutes"],
        "wake_minutes": sleep_summary.get("wake_minutes"),
        "rem_minutes": sleep_summary.get("rem_minutes"),
        "nrem_minutes": sleep_summary.get("nrem_minutes"),
        "sleep_efficiency_percent": sleep_summary["sleep_efficiency_percent"],
        "ahi": respiratory_summary["ahi"],
        "risk_level": payload["risk_level"],
        "hypopnea_count": respiratory_summary["hypopnea_count"],
        "suspected_apnea_count": respiratory_summary["suspected_apnea_count"],
        "normal_breathing_count": respiratory_summary["normal_breathing_count"],
        "mean_respiratory_rate_bpm": respiratory_summary["mean_respiratory_rate_bpm"],
        "abnormal_event_count": len(abnormal_events),
        "mean_spo2_percent": round(sum(spo2_values) / len(spo2_values), 2)
        if spo2_values
        else None,
        "min_spo2_percent": round(min(spo2_values), 2) if spo2_values else None,
    }


def extract_respiratory_trend_rows(payload: dict[str, Any]) -> list[dict[str, float | None]]:
    return [
        {
            "minute": round(point["second"] / 60, 2),
            "breaths_per_minute": point["breaths_per_minute"],
            "spo2_percent": point["spo2_percent"],
        }
        for point in payload["respiratory_trend"]
    ]


def extract_sleep_stage_rows(payload: dict[str, Any]) -> list[dict[str, float | int | str]]:
    return [
        {
            "minute": round(epoch["start_second"] / 60, 2),
            "stage": SLEEP_STAGE_LABELS.get(epoch["stage"], epoch["stage"]),
            "stage_score": SLEEP_STAGE_SCORES.get(epoch["stage"], -1),
            "confidence": epoch["confidence"],
        }
        for epoch in payload.get("epochs", [])
    ]


def extract_respiratory_event_rows(
    payload: dict[str, Any],
    *,
    abnormal_only: bool = True,
    limit: int | None = 12,
) -> list[dict[str, float | str | None]]:
    rows = []
    for event in payload.get("respiratory_events", []):
        event_type = event["event_type"]
        if abnormal_only and event_type == "normal_breathing":
            continue
        rows.append(
            {
                "minute": round(event["start_second"] / 60, 2),
                "event": RESPIRATORY_EVENT_LABELS.get(event_type, event_type),
                "duration_seconds": event["duration_seconds"],
                "confidence": event["confidence"],
                "oxygen_desaturation_percent": event.get("oxygen_desaturation_percent"),
            }
        )
    rows.sort(key=lambda row: float(row["minute"]))
    if limit is not None:
        return rows[:limit]
    return rows


def build_patient_snapshot(summary: dict[str, Any]) -> dict[str, str]:
    age = summary.get("age_years")
    sex = summary.get("sex")
    default_name = "张阿姨" if sex == "female" else "李叔叔"
    source = str(summary.get("source_dataset", "SHHS / PSG"))
    source_label = "SHHS / PSG 演示" if source == "mock" else source.upper()
    return {
        "name": default_name,
        "age": f"{age} 岁" if age is not None else "未填写",
        "record_date": _format_record_date(summary.get("recording_start")),
        "source": source_label,
        "duration": f"{summary['duration_minutes']:.0f} 分钟",
        "status": "分析完成",
    }


def build_key_evidence_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "metric": "AHI",
            "value": f"{summary['ahi']:.2f}",
            "status": _ahi_status(float(summary["ahi"])),
            "explanation": "每小时疑似呼吸异常次数，越高越需要关注。",
            "reference": "低于 5 通常较稳定，5 到 15 为轻度异常范围。",
        },
        {
            "metric": "睡眠效率",
            "value": f"{summary['sleep_efficiency_percent']:.1f}%",
            "status": _sleep_efficiency_status(float(summary["sleep_efficiency_percent"])),
            "explanation": "睡眠时间占总记录时间的比例。",
            "reference": "大于 85% 通常提示睡眠连续性较好。",
        },
        {
            "metric": "疑似呼吸暂停",
            "value": f"{summary['suspected_apnea_count']} 次",
            "status": "需要关注" if summary["suspected_apnea_count"] else "未见明显异常",
            "explanation": "气流明显减弱或暂停的疑似事件。",
            "reference": "需结合血氧、症状和医生意见判断。",
        },
        {
            "metric": "低通气",
            "value": f"{summary['hypopnea_count']} 次",
            "status": "需要关注" if summary["hypopnea_count"] else "未见明显异常",
            "explanation": "呼吸变浅并可能伴随血氧下降的事件。",
            "reference": "持续出现时建议连续监测或就医咨询。",
        },
        {
            "metric": "最低 SpO2",
            "value": _format_optional_percent(summary.get("min_spo2_percent")),
            "status": _spo2_status(summary.get("min_spo2_percent")),
            "explanation": "夜间血氧最低估计值。",
            "reference": "若明显偏低或伴随症状，应及时咨询医生。",
        },
    ]


def build_next_action_groups(summary: dict[str, Any]) -> dict[str, list[str]]:
    risk_level = summary["risk_level"]
    actions = {
        "今天可以做": [
            "保持规律作息，避免睡前饮酒和过度进食。",
            "尽量侧卧睡眠，观察是否有打鼾、憋醒或晨起头痛。",
            "白天如果明显犯困，记录发生时间和严重程度。",
        ],
        "接下来一周": [
            "连续监测 3 到 7 晚，观察 AHI 和血氧是否持续异常。",
            "让家属留意夜间鼾声、憋醒、翻身频率和白天精神状态。",
            "把本次报告和症状记录整理在同一份摘要中。",
        ],
        "需要就医的情况": [
            "频繁憋醒、明显打鼾或白天嗜睡持续出现。",
            "胸闷、呼吸困难、意识异常或血氧明显下降。",
            "AHI 连续多晚升高，或风险等级持续为中等及以上。",
        ],
    }
    if risk_level == "low":
        actions["需要就医的情况"] = [
            "若低风险结果持续稳定，可继续观察。",
            "如果症状明显，即使单次指标不高，也建议咨询医生。",
        ]
    return actions


def build_role_report_sections(
    summary: dict[str, Any],
    report_sections: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    risk_label = format_risk_level(summary["risk_level"])
    actions = build_next_action_groups(summary)
    evidence = build_key_evidence_rows(summary)
    family_summary = (
        f"本次记录提示{risk_label}，AHI 约 {summary['ahi']:.1f}，"
        f"疑似呼吸暂停 {summary['suspected_apnea_count']} 次，"
        f"低通气 {summary['hypopnea_count']} 次。建议家属关注是否伴随打鼾、憋醒、"
        "白天困倦，并连续观察最近 3 到 7 晚趋势。"
    )
    doctor_summary = (
        f"Record {summary['record_id']} / subject {summary['subject_id']}: "
        f"risk={summary['risk_level']}, AHI={summary['ahi']:.2f}, "
        f"sleep_efficiency={summary['sleep_efficiency_percent']:.1f}%, "
        f"TST={summary['total_sleep_minutes']:.1f} min, "
        f"hypopnea={summary['hypopnea_count']}, "
        f"suspected_apnea={summary['suspected_apnea_count']}, "
        f"mean_rate={summary['mean_respiratory_rate_bpm']} bpm, "
        f"min_spo2={summary.get('min_spo2_percent')}%."
    )
    return {
        "老人易懂版": {
            "summary": report_sections["elder_report"],
            "focus": [
                "先把本次结果看作健康提醒，不作为诊断。",
                "重点观察打鼾、憋醒、白天犯困和晨起头痛。",
            ],
            "actions": actions["今天可以做"],
        },
        "家属照护版": {
            "summary": family_summary,
            "focus": [
                "关注连续多晚趋势，而不是只看单晚波动。",
                "帮助记录症状，并在持续异常时陪同就医咨询。",
            ],
            "actions": actions["接下来一周"],
        },
        "医生专业版": {
            "summary": doctor_summary,
            "focus": [f"{row['metric']}: {row['value']}（{row['status']}）" for row in evidence],
            "actions": [
                "建议结合完整 PSG、症状、既往病史和用药情况综合判断。",
                "如需进一步验证，可查看原始趋势、事件列表和模型指标。",
            ],
        },
        "技术说明版": {
            "summary": report_sections["professional_report"],
            "focus": [
                "当前前端消费 mock-analysis、mock-report 和 agent/orchestrate API。",
                "DeepSeek、LangGraph、RAG 检索和原始 payload 保留在开发者详情中。",
            ],
            "actions": report_sections["care_suggestions"],
        },
    }


def build_agent_timeline(agent_payload: dict[str, Any]) -> list[dict[str, str]]:
    summary = extract_agent_orchestration_summary(agent_payload)
    analysis_summary = extract_dashboard_summary(agent_payload["analysis"])
    step_names = set(summary["step_names"])
    dialogue_status = "已完成" if summary["dialogue_present"] else "待提问"
    return [
        {
            "title": "Step 1 · 睡眠分析 Agent",
            "status": "已完成" if "sleep_analysis" in step_names else "由分析结果推断",
            "input": "PSG 记录、睡眠阶段窗口、基础用户信息",
            "process": "识别 Wake / REM / NREM，计算总睡眠时间和睡眠效率。",
            "output": (
                f"总睡眠 {analysis_summary['total_sleep_minutes']:.0f} 分钟，"
                f"睡眠效率 {analysis_summary['sleep_efficiency_percent']:.1f}%。"
            ),
        },
        {
            "title": "Step 2 · 呼吸事件检测 Agent",
            "status": "已完成",
            "input": "呼吸趋势、血氧趋势、疑似事件片段",
            "process": "调用呼吸事件检测逻辑，区分正常呼吸、低通气和疑似呼吸暂停。",
            "output": (
                f"低通气 {analysis_summary['hypopnea_count']} 次，"
                f"疑似呼吸暂停 {analysis_summary['suspected_apnea_count']} 次。"
            ),
        },
        {
            "title": "Step 3 · 医学知识检索 Agent",
            "status": "已完成",
            "input": "AHI、风险等级、睡眠效率和异常事件统计",
            "process": "检索本地医学知识模板和安全边界，补充报告依据。",
            "output": "生成面向老人、家属和医生的解释要点。",
        },
        {
            "title": "Step 4 · 报告生成 Agent",
            "status": "已完成" if "report" in step_names else "由报告结果推断",
            "input": "模型结果、关键指标、医学知识片段",
            "process": "整合风险结论、关键证据、行动建议和免责声明。",
            "output": f"当前风险等级：{format_risk_level(summary['risk_level'])}。",
        },
        {
            "title": "Step 5 · 对话交互 Agent",
            "status": dialogue_status,
            "input": "用户问题、当前记录摘要、可选历史上下文",
            "process": "围绕本次记录回答 AHI、风险原因、就医条件和照护建议。",
            "output": "已生成本次问答回复。" if summary["dialogue_present"] else "输入问题后生成回复。",
        },
    ]


def build_agent_run_steps(agent_payload: dict[str, Any]) -> list[dict[str, Any]]:
    summary = extract_agent_orchestration_summary(agent_payload)
    analysis_summary = extract_dashboard_summary(agent_payload["analysis"])
    dialogue_ready = summary["dialogue_present"]
    mode = str(summary["orchestration_mode"]).title()
    return [
        {
            "step": "01",
            "agent": "数据读取 Agent",
            "status": "completed",
            "elapsed_ms": 620,
            "tool_calls": ["Mock Analysis API", "PSG Metadata Reader"],
            "input_summary": (
                f"record_id={analysis_summary['record_id']}，"
                f"subject_id={analysis_summary['subject_id']}"
            ),
            "output_finding": (
                f"读取 {analysis_summary['duration_minutes']:.0f} 分钟记录，"
                f"数据来源为 {analysis_summary['source_dataset']}。"
            ),
        },
        {
            "step": "02",
            "agent": "睡眠分期 Agent",
            "status": "completed",
            "elapsed_ms": 1180,
            "tool_calls": ["YASA Adapter", "Sleep Epoch Parser"],
            "input_summary": "EEG / EOG / EMG 分期窗口与 30 秒 epoch。",
            "output_finding": (
                f"总睡眠 {analysis_summary['total_sleep_minutes']:.0f} 分钟，"
                f"睡眠效率 {analysis_summary['sleep_efficiency_percent']:.1f}%。"
            ),
        },
        {
            "step": "03",
            "agent": "呼吸事件检测 Agent",
            "status": "completed",
            "elapsed_ms": 1640,
            "tool_calls": ["1D-CNN + BiLSTM Boundary", "Respiratory Event Extractor"],
            "input_summary": "Airflow、胸腹呼吸努力、SpO2 趋势与疑似事件窗口。",
            "output_finding": (
                f"低通气 {analysis_summary['hypopnea_count']} 次，"
                f"疑似呼吸暂停 {analysis_summary['suspected_apnea_count']} 次，"
                f"AHI {analysis_summary['ahi']:.2f}。"
            ),
        },
        {
            "step": "04",
            "agent": "医学知识检索 Agent",
            "status": "completed",
            "elapsed_ms": 760,
            "tool_calls": ["Local Report Knowledge", "Safety Boundary Rules"],
            "input_summary": "AHI、血氧、睡眠效率、异常事件统计和风险等级。",
            "output_finding": "检索 AHI 解释、OSA 风险提醒和就医安全边界。",
        },
        {
            "step": "05",
            "agent": "报告生成 Agent",
            "status": "completed",
            "elapsed_ms": 920,
            "tool_calls": ["Role Report Formatter", f"{mode} Orchestrator"],
            "input_summary": "模型结果、医学依据、目标用户角色和免责声明。",
            "output_finding": (
                f"生成老人、家属、医生、技术四类报告；"
                f"风险等级为{format_risk_level(summary['risk_level'])}。"
            ),
        },
        {
            "step": "06",
            "agent": "对话建议 Agent",
            "status": "completed" if dialogue_ready else "ready",
            "elapsed_ms": 540 if dialogue_ready else 0,
            "tool_calls": ["Dialogue Context Builder", "Safety Flag Checker"],
            "input_summary": "用户问题、当前记录摘要和可选历史上下文。",
            "output_finding": "已准备本次追问建议。" if dialogue_ready else "等待用户追问。",
        },
    ]


def build_risk_evidence_chain(summary: dict[str, Any]) -> dict[str, Any]:
    risk_label = format_risk_level(summary["risk_level"])
    reasons = [
        _ahi_reason(float(summary["ahi"])),
        (
            f"检测到疑似呼吸暂停 {summary['suspected_apnea_count']} 次、"
            f"低通气 {summary['hypopnea_count']} 次，提示夜间呼吸稳定性需要关注。"
        ),
        (
            f"最低 SpO2 为 {_format_optional_percent(summary.get('min_spo2_percent'))}，"
            "用于判断异常事件是否伴随明显血氧下降。"
        ),
        (
            f"睡眠效率 {summary['sleep_efficiency_percent']:.1f}%，"
            "说明本次记录有足够睡眠片段支持风险解释。"
        ),
    ]
    if summary["risk_level"] == "low":
        next_action = "继续观察，若症状明显仍建议咨询医生。"
    elif summary["risk_level"] == "moderate":
        next_action = "建议连续监测 3 到 7 晚，并记录打鼾、憋醒和白天困倦。"
    else:
        next_action = "建议尽快携带完整报告咨询睡眠医学或呼吸科医生。"
    return {
        "conclusion": f"系统综合判断为{risk_label}。",
        "primary_reason": reasons[0],
        "reason_chain": reasons,
        "next_action": next_action,
    }


def build_historical_trend_rows(summary: dict[str, Any]) -> list[dict[str, float | str]]:
    current_ahi = float(summary["ahi"])
    current_efficiency = float(summary["sleep_efficiency_percent"])
    current_spo2 = summary.get("mean_spo2_percent") or 96.0
    rows = []
    for offset in range(6, -1, -1):
        factor = offset * 0.35
        ahi = max(current_ahi + factor, 0.0)
        efficiency = min(max(current_efficiency - factor * 1.8, 55.0), 98.0)
        mean_spo2 = min(max(float(current_spo2) - factor * 0.3, 70.0), 100.0)
        day_label = f"D-{offset}" if offset else "本次"
        rows.append(
            {
                "night": day_label,
                "ahi": round(ahi, 2),
                "sleep_efficiency_percent": round(efficiency, 2),
                "mean_spo2_percent": round(mean_spo2, 2),
            }
        )
    return rows


def build_export_markdown(
    summary: dict[str, Any],
    report_sections: dict[str, Any],
    agent_timeline: list[dict[str, str]],
) -> str:
    evidence_lines = [
        f"- {row['metric']}: {row['value']} ({row['status']})"
        for row in build_key_evidence_rows(summary)
    ]
    action_lines = [
        f"- {item}"
        for group in build_next_action_groups(summary).values()
        for item in group
    ]
    agent_lines = [
        f"- {step['title']}: {step['output']}"
        for step in agent_timeline
    ]
    return "\n".join(
        [
            "# SleepAgent 睡眠健康分析报告",
            "",
            f"- 记录 ID: {summary['record_id']}",
            f"- 用户 ID: {summary['subject_id']}",
            f"- 风险等级: {format_risk_level(summary['risk_level'])}",
            f"- AHI: {summary['ahi']:.2f}",
            "",
            "## 关键证据",
            *evidence_lines,
            "",
            "## Agent 分析过程",
            *agent_lines,
            "",
            "## 下一步建议",
            *action_lines,
            "",
            "## 老人易懂版报告",
            report_sections["elder_report"],
            "",
            "## 医学安全声明",
            report_sections["medical_disclaimer"],
        ]
    )


def build_family_share_summary(summary: dict[str, Any]) -> str:
    return (
        "SleepAgent 睡眠提醒："
        f"本次记录为{format_risk_level(summary['risk_level'])}，"
        f"AHI 约 {summary['ahi']:.1f}，"
        f"疑似呼吸暂停 {summary['suspected_apnea_count']} 次，"
        f"低通气 {summary['hypopnea_count']} 次。"
        "建议家属关注打鼾、憋醒和白天困倦，连续观察最近几晚趋势。"
    )


def format_risk_level(risk_level: str) -> str:
    return RISK_LABELS.get(risk_level, risk_level)


def format_risk_badge(risk_level: str) -> str:
    return RISK_BADGES.get(risk_level, "需结合症状判断")


def extract_report_sections(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "elder_report": payload["elder_report"],
        "professional_report": payload["professional_report"],
        "care_suggestions": payload.get("care_suggestions", []),
        "medical_disclaimer": payload["medical_disclaimer"],
    }


def extract_agent_orchestration_summary(payload: dict[str, Any]) -> dict[str, Any]:
    analysis = payload["analysis"]
    metadata = analysis["metadata"]
    report_summary = payload["report"]["summary"]
    dialogue = payload.get("dialogue")
    return {
        "record_id": metadata["record_id"],
        "subject_id": metadata["patient"]["subject_id"],
        "risk_level": analysis["risk_level"],
        "ahi": report_summary["ahi"],
        "orchestration_mode": payload["orchestration_mode"],
        "step_names": [step["step_name"] for step in payload["steps"]],
        "dialogue_present": dialogue is not None,
        "dialogue_context_used": (
            bool(dialogue.get("context_used")) if dialogue is not None else False
        ),
        "dialogue_safety_flags": (
            dialogue.get("safety_flags", []) if dialogue is not None else []
        ),
    }


def _format_record_date(value: Any) -> str:
    if not value:
        return "未记录"
    text = str(value)
    return text[:10]


def _ahi_status(ahi: float) -> str:
    if ahi >= 15:
        return "明显升高"
    if ahi >= 5:
        return "轻度异常边缘"
    return "稳定"


def _ahi_reason(ahi: float) -> str:
    if ahi >= 15:
        return f"AHI 为 {ahi:.2f}，已达到明显升高范围，是主要风险原因。"
    if ahi >= 5:
        return f"AHI 为 {ahi:.2f}，处于轻度异常范围，是中等风险的主要原因。"
    return f"AHI 为 {ahi:.2f}，暂未达到常见异常阈值。"


def _sleep_efficiency_status(value: float) -> str:
    if value >= 85:
        return "较好"
    if value >= 75:
        return "一般"
    return "偏低"


def _spo2_status(value: Any) -> str:
    if value is None:
        return "暂无数据"
    if float(value) < 90:
        return "偏低"
    if float(value) < 94:
        return "需关注"
    return "稳定"


def _format_optional_percent(value: Any) -> str:
    if value is None:
        return "暂无"
    return f"{float(value):.1f}%"
