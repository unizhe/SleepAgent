from frontend.api_client import (
    build_agent_run_steps,
    build_agent_timeline,
    build_export_markdown,
    build_family_share_summary,
    build_historical_trend_rows,
    build_key_evidence_rows,
    build_agent_orchestration_url,
    build_mock_analysis_url,
    build_mock_report_llm_url,
    build_mock_report_url,
    build_patient_snapshot,
    build_risk_evidence_chain,
    build_role_report_sections,
    extract_agent_orchestration_summary,
    extract_dashboard_summary,
    extract_respiratory_event_rows,
    extract_report_sections,
    extract_respiratory_trend_rows,
    extract_sleep_stage_rows,
    format_risk_badge,
    format_risk_level,
    fetch_agent_orchestration,
    fetch_mock_report_llm,
)


def test_build_mock_analysis_url_strips_trailing_slash() -> None:
    assert build_mock_analysis_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000/mock-analysis"
    )


def test_build_mock_report_url_strips_trailing_slash() -> None:
    assert build_mock_report_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000/mock-report"
    )


def test_build_mock_report_llm_url_strips_trailing_slash() -> None:
    assert build_mock_report_llm_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000/mock-report/llm"
    )


def test_build_agent_orchestration_url_strips_trailing_slash() -> None:
    assert build_agent_orchestration_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000/agent/orchestrate"
    )


def test_fetch_mock_report_llm_sends_explicit_deepseek_opt_in(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"ok": True}

    def fake_get(url, *, params, timeout):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("frontend.api_client.httpx.get", fake_get)

    payload = fetch_mock_report_llm(
        api_base_url="http://127.0.0.1:8000/",
        record_id="record-llm",
        subject_id="subject-llm",
        duration_hours=0.5,
        seed=7,
        abnormal_event_rate_per_hour=8.0,
        use_deepseek=True,
        timeout_seconds=9.0,
    )

    assert payload == {"ok": True}
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/mock-report/llm",
            "params": {
                "record_id": "record-llm",
                "subject_id": "subject-llm",
                "duration_hours": 0.5,
                "seed": 7,
                "abnormal_event_rate_per_hour": 8.0,
                "use_deepseek": True,
            },
            "timeout": 9.0,
        }
    ]


def test_fetch_agent_orchestration_posts_formal_agent_body(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"ok": True}

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("frontend.api_client.httpx.post", fake_post)

    payload = fetch_agent_orchestration(
        api_base_url="http://127.0.0.1:8000/",
        record_id="agent-record",
        subject_id="agent-subject",
        duration_hours=0.5,
        seed=8,
        abnormal_event_rate_per_hour=9.0,
        user_question="AHI 是什么？",
        dialogue_context={"history_summary": "最近一周睡眠效率下降"},
        use_deepseek_report=False,
        use_langgraph=True,
        timeout_seconds=11.0,
    )

    assert payload == {"ok": True}
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/agent/orchestrate",
            "json": {
                "record_id": "agent-record",
                "subject_id": "agent-subject",
                "duration_hours": 0.5,
                "seed": 8,
                "abnormal_event_rate_per_hour": 9.0,
                "user_question": "AHI 是什么？",
                "dialogue_context": {"history_summary": "最近一周睡眠效率下降"},
                "use_deepseek_report": False,
                "use_langgraph": True,
            },
            "timeout": 11.0,
        }
    ]


def test_extract_dashboard_summary_uses_backend_payload_shape() -> None:
    payload = _payload()

    summary = extract_dashboard_summary(payload)

    assert summary == {
        "record_id": "record-1",
        "subject_id": "subject-1",
        "source_dataset": "mock",
        "age_years": 68,
        "sex": "female",
        "recording_start": "2026-01-01T22:00:00Z",
        "duration_minutes": 30.0,
        "total_sleep_minutes": 24.0,
        "wake_minutes": 6.0,
        "rem_minutes": 4.0,
        "nrem_minutes": 20.0,
        "sleep_efficiency_percent": 80.0,
        "ahi": 6.5,
        "risk_level": "moderate",
        "hypopnea_count": 2,
        "suspected_apnea_count": 1,
        "normal_breathing_count": 3,
        "mean_respiratory_rate_bpm": 14.2,
        "abnormal_event_count": 2,
        "mean_spo2_percent": 96.0,
        "min_spo2_percent": 95.8,
    }


def test_extract_respiratory_trend_rows_converts_seconds_to_minutes() -> None:
    rows = extract_respiratory_trend_rows(_payload())

    assert rows == [
        {"minute": 0.0, "breaths_per_minute": 13.8, "spo2_percent": 96.2},
        {"minute": 5.0, "breaths_per_minute": 14.4, "spo2_percent": 95.8},
    ]


def test_extract_sleep_stage_rows_maps_stage_names_for_charting() -> None:
    rows = extract_sleep_stage_rows(_payload())

    assert rows == [
        {"minute": 0.0, "stage": "清醒", "stage_score": 2, "confidence": 0.82},
        {"minute": 0.5, "stage": "NREM", "stage_score": 0, "confidence": 0.91},
        {"minute": 1.0, "stage": "REM", "stage_score": 1, "confidence": 0.86},
    ]


def test_extract_respiratory_event_rows_returns_abnormal_markers() -> None:
    rows = extract_respiratory_event_rows(_payload(), limit=None)

    assert rows == [
        {
            "minute": 3.0,
            "event": "低通气",
            "duration_seconds": 18.0,
            "confidence": 0.8,
            "oxygen_desaturation_percent": 3.4,
        },
        {
            "minute": 8.0,
            "event": "疑似呼吸暂停",
            "duration_seconds": 20.0,
            "confidence": 0.76,
            "oxygen_desaturation_percent": 5.2,
        },
    ]


def test_extract_report_sections_uses_backend_payload_shape() -> None:
    payload = {
        "elder_report": "老人易懂版报告",
        "professional_report": "子女/医生专业版报告",
        "care_suggestions": ["继续观察睡眠趋势"],
        "medical_disclaimer": "仅用于科研原型展示。",
    }

    sections = extract_report_sections(payload)

    assert sections == payload


def test_product_view_helpers_build_role_specific_frontend_sections() -> None:
    summary = extract_dashboard_summary(_payload())
    report_sections = _report_sections()

    patient = build_patient_snapshot(summary)
    evidence = build_key_evidence_rows(summary)
    role_sections = build_role_report_sections(summary, report_sections)
    trend_rows = build_historical_trend_rows(summary)
    share_summary = build_family_share_summary(summary)

    assert patient == {
        "name": "张阿姨",
        "age": "68 岁",
        "record_date": "2026-01-01",
        "source": "SHHS / PSG 演示",
        "duration": "30 分钟",
        "status": "分析完成",
    }
    assert evidence[0] == {
        "metric": "AHI",
        "value": "6.50",
        "status": "轻度异常边缘",
        "explanation": "每小时疑似呼吸异常次数，越高越需要关注。",
        "reference": "低于 5 通常较稳定，5 到 15 为轻度异常范围。",
    }
    assert "本次记录提示中等风险" in role_sections["家属照护版"]["summary"]
    assert "Record record-1 / subject subject-1" in role_sections["医生专业版"]["summary"]
    assert trend_rows[-1]["night"] == "本次"
    assert trend_rows[-1]["ahi"] == 6.5
    assert share_summary.startswith("SleepAgent 睡眠提醒：本次记录为中等风险")
    assert format_risk_level("moderate") == "中等风险"
    assert format_risk_badge("moderate") == "需要关注"


def test_extract_agent_orchestration_summary_uses_backend_payload_shape() -> None:
    payload = _agent_payload()

    summary = extract_agent_orchestration_summary(payload)

    assert summary == {
        "record_id": "agent-record",
        "subject_id": "agent-subject",
        "risk_level": "moderate",
        "ahi": 6.5,
        "orchestration_mode": "linear",
        "step_names": ["sleep_analysis", "report", "dialogue"],
        "dialogue_present": True,
        "dialogue_context_used": True,
        "dialogue_safety_flags": ["urgent_symptom_safety_boundary"],
    }


def test_agent_timeline_and_export_report_are_grounded_in_current_payload() -> None:
    agent_payload = _agent_payload()
    summary = extract_dashboard_summary(agent_payload["analysis"])
    report_sections = extract_report_sections(agent_payload["report"])
    timeline = build_agent_timeline(agent_payload)
    export = build_export_markdown(summary, report_sections, timeline)

    assert timeline[0]["title"] == "Step 1 · 睡眠分析 Agent"
    assert timeline[0]["output"] == "总睡眠 24 分钟，睡眠效率 80.0%。"
    assert timeline[-1]["status"] == "已完成"
    assert "风险等级: 中等风险" in export
    assert "低通气 2 次，疑似呼吸暂停 1 次。" in export


def test_agent_run_steps_and_evidence_chain_support_dynamic_console() -> None:
    agent_payload = _agent_payload()
    summary = extract_dashboard_summary(agent_payload["analysis"])

    run_steps = build_agent_run_steps(agent_payload)
    evidence_chain = build_risk_evidence_chain(summary)

    assert [step["agent"] for step in run_steps] == [
        "数据读取 Agent",
        "睡眠分期 Agent",
        "呼吸事件检测 Agent",
        "医学知识检索 Agent",
        "报告生成 Agent",
        "对话建议 Agent",
    ]
    assert run_steps[2]["tool_calls"] == [
        "1D-CNN + BiLSTM Boundary",
        "Respiratory Event Extractor",
    ]
    assert run_steps[2]["output_finding"] == "低通气 2 次，疑似呼吸暂停 1 次，AHI 6.50。"
    assert evidence_chain == {
        "conclusion": "系统综合判断为中等风险。",
        "primary_reason": "AHI 为 6.50，处于轻度异常范围，是中等风险的主要原因。",
        "reason_chain": [
            "AHI 为 6.50，处于轻度异常范围，是中等风险的主要原因。",
            "检测到疑似呼吸暂停 1 次、低通气 2 次，提示夜间呼吸稳定性需要关注。",
            "最低 SpO2 为 95.8%，用于判断异常事件是否伴随明显血氧下降。",
            "睡眠效率 80.0%，说明本次记录有足够睡眠片段支持风险解释。",
        ],
        "next_action": "建议连续监测 3 到 7 晚，并记录打鼾、憋醒和白天困倦。",
    }


def _payload() -> dict:
    return {
        "metadata": {
            "record_id": "record-1",
            "source_dataset": "mock",
            "recording_start": "2026-01-01T22:00:00Z",
            "patient": {
                "subject_id": "subject-1",
                "age_years": 68,
                "sex": "female",
            },
        },
        "epochs": [
            {
                "start_second": 0.0,
                "duration_seconds": 30.0,
                "stage": "Wake",
                "confidence": 0.82,
            },
            {
                "start_second": 30.0,
                "duration_seconds": 30.0,
                "stage": "NREM",
                "confidence": 0.91,
            },
            {
                "start_second": 60.0,
                "duration_seconds": 30.0,
                "stage": "REM",
                "confidence": 0.86,
            },
        ],
        "sleep_summary": {
            "total_recording_minutes": 30.0,
            "total_sleep_time_minutes": 24.0,
            "wake_minutes": 6.0,
            "rem_minutes": 4.0,
            "nrem_minutes": 20.0,
            "sleep_efficiency_percent": 80.0,
        },
        "respiratory_summary": {
            "ahi": 6.5,
            "hypopnea_count": 2,
            "suspected_apnea_count": 1,
            "normal_breathing_count": 3,
            "mean_respiratory_rate_bpm": 14.2,
        },
        "respiratory_trend": [
            {"second": 0.0, "breaths_per_minute": 13.8, "spo2_percent": 96.2},
            {"second": 300.0, "breaths_per_minute": 14.4, "spo2_percent": 95.8},
        ],
        "respiratory_events": [
            {
                "start_second": 60.0,
                "duration_seconds": 45.0,
                "event_type": "normal_breathing",
                "confidence": 0.95,
                "oxygen_desaturation_percent": None,
            },
            {
                "start_second": 180.0,
                "duration_seconds": 18.0,
                "event_type": "hypopnea",
                "confidence": 0.8,
                "oxygen_desaturation_percent": 3.4,
            },
            {
                "start_second": 480.0,
                "duration_seconds": 20.0,
                "event_type": "suspected_apnea",
                "confidence": 0.76,
                "oxygen_desaturation_percent": 5.2,
            },
        ],
        "risk_level": "moderate",
    }


def _report_sections() -> dict:
    return {
        "elder_report": "老人易懂版报告",
        "professional_report": "子女/医生专业版报告",
        "care_suggestions": ["继续观察睡眠趋势"],
        "medical_disclaimer": "仅用于科研原型展示。",
        "summary": {
            "record_id": "record-1",
            "subject_id": "subject-1",
            "risk_level": "moderate",
            "ahi": 6.5,
        },
    }


def _agent_payload() -> dict:
    analysis = _payload()
    analysis["metadata"]["record_id"] = "agent-record"
    analysis["metadata"]["patient"]["subject_id"] = "agent-subject"
    return {
        "analysis": analysis,
        "report": _report_sections(),
        "dialogue": {
            "context_used": True,
            "safety_flags": ["urgent_symptom_safety_boundary"],
        },
        "steps": [
            {"step_name": "sleep_analysis"},
            {"step_name": "report"},
            {"step_name": "dialogue"},
        ],
        "orchestration_mode": "linear",
    }
