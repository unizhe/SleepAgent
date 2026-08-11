from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_PATH = PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
TYPES_PATH = PROJECT_ROOT / "frontend/lib/radar-types.ts"
GLOBALS_PATH = PROJECT_ROOT / "frontend/app/globals.css"


def _workspace() -> str:
    return WORKSPACE_PATH.read_text(encoding="utf-8")


def test_dashboard_exposes_all_required_quality_fields() -> None:
    source = _workspace()
    types = TYPES_PATH.read_text(encoding="utf-8")

    for label in ("覆盖率", "无效读数", "离床时段", "不在床时段", "设备状态"):
        assert label in source
    assert "QualitySummaryBar" in source
    assert "out_of_bed_intervals?: string[]" in types
    assert "not_in_bed_intervals?: string[]" in types
    assert "deviceStatusLabel" in source
    assert "intervalSummary" in source


def test_daily_disclosure_is_small_and_details_live_in_a_drawer() -> None:
    source = _workspace()

    assert "SafetyFootnote" in source
    assert 'text-[11px] leading-5 text-slate-500' in source
    assert "关于人工智能分析 / 数据说明" in source
    assert "关于 AI 分析 / 数据说明" not in source
    assert "SafetyDisclosure" in source
    assert 'role="dialog"' in source
    assert 'event.key === "Escape"' in source
    assert "autoFocus" in source
    assert "不提供确诊、精确呼吸暂停低通气指数、处方或药物建议" in source


def test_role_specific_safety_copy_and_sources_are_visible() -> None:
    source = _workspace()

    assert "如不适明显，请让家人协助联系医生" in source
    assert "FamilyRiskCard" in source
    assert 'risk === "watch" || risk === "escalate"' in source
    assert "提示：{localizedGeneratedText(caveat" in source
    assert "来源：{source ? \"当前任务证据台账\" : \"规则与标准化数据\"}" in source
    assert "完整注意事项" in source
    assert "(report?.caveats ?? []).map" in source
    assert "(report?.safety_notices ?? []).map" in source


def test_urgent_boundary_has_prominent_alert_and_action() -> None:
    source = _workspace()

    assert 'detail?.risk_level === "urgent_boundary"' in source
    assert "UrgentBoundaryBanner" in source
    assert 'role="alert"' in source
    assert 'href="tel:120"' in source
    assert "拨打 120" in source
    assert "先处理急症线索，不要等待睡眠解释" in source


def test_motion_is_scoped_and_reduced_motion_is_global() -> None:
    source = _workspace()
    globals_source = GLOBALS_PATH.read_text(encoding="utf-8")

    assert "useReducedMotion" in source
    assert "prefersReducedMotion ? false" in source
    assert source.count("<motion.") == 1
    for forbidden in ("whileHover", "whileTap", "animate-pulse", "transition-all"):
        assert forbidden not in source
    assert "@media (prefers-reduced-motion: reduce)" in globals_source
    assert "animation-duration: 0.01ms !important" in globals_source
    assert "transition-duration: 0.01ms !important" in globals_source


def test_v1_visible_copy_is_chinese_except_sleepagent_and_agent_terms() -> None:
    source = _workspace()

    assert "SleepAgent 雷达" in source
    assert "多 Agent 分析过程" in source
    for forbidden in (
        "SleepAgent Radar",
        "Multi-Agent 过程",
        "Evidence Ledger",
        "reviewed RAG",
        "% confidence",
        "完整 caveat",
        "日常 Dashboard",
        "facts_mutated=false",
        "Task {",
        "Trace {",
    ):
        assert forbidden not in source
    assert "localizedGeneratedText(response.answer" in source
    assert "eventDisplayMessage(event)" in source
