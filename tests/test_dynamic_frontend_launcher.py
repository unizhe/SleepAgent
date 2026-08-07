from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "frontend/components/radar/DynamicRadarWorkspace.tsx"
API = ROOT / "frontend/lib/radar-api.ts"


def test_clean_home_loads_realtime_and_active_tasks_without_creating_analysis() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")
    effect = source[source.index("useEffect(() => {") : source.index("  }, []);")]

    assert "listRadarDevices()" in effect
    assert "getRadarDashboard" in effect
    assert 'listRadarTasks("active")' in effect
    assert "setActiveTasks(resumable)" in effect
    assert "setTask(resumable)" not in effect
    assert "createRadarGoalTask" not in effect
    assert "runRadarTask" not in effect
    assert "刷新页面不会自动生成报告" in source
    assert "继续分析 {goalDate(item)}" in source


def test_goal_launcher_requires_exact_scope_and_uses_product_episode_contract() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")

    assert 'type="date"' in source
    assert "usesTargetDate" in source
    assert "usesRange" in source
    assert "sourceArtifactId" in source
    assert "必须明确指定，不使用‘最近一份’" in source
    assert 'runtime_kind: "product_episode"' in api
    assert "goal_type: goalType" in api
    assert "target_date: targetDate" in api
    assert "range_start: rangeStart" in api


def test_decision_journal_uses_persisted_events_not_fake_progress() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")

    assert "getRadarDecisionTrace" in source
    assert "DecisionJournal" in source
    assert "真实事件" in source
    assert "14/14" not in source
    assert "fake" not in source.lower()
    assert "decision-trace" in api


def test_dynamic_status_exposes_real_mode_events_and_safe_next_step() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")

    assert "data-execution-mode" in source
    for event_type in (
        "plan.rejected",
        "task.partial",
        "task.blocked",
        "confirmation.action_completed",
        "confirmation.resolved",
    ):
        assert event_type in source
    assert 'aria-label="安全下一步"' in source
    assert "detail.completion_receipt.safe_next_step" in source


def test_product_episode_publication_and_receipt_are_rendered_directly() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")
    types = (
        ROOT / "frontend" / "lib" / "radar-types.ts"
    ).read_text(encoding="utf-8")

    assert 'item.artifact_type === "product_episode_result"' in source
    assert "payload.publication" in source
    assert "RadarProductEpisodeReceipt" in types
    assert '"deterministic_only"' in types


def test_user_input_is_reviewed_task_continuation_and_history_is_explicit() -> None:
    source = WORKSPACE.read_text(encoding="utf-8")

    assert "pending_user_input_request_id" in source
    assert "answerRadarUserInput" in source
    assert "Agent 需要一个可观察事实" in source
    assert "declineRadarUserInput" in source
    assert "暂不回答并停止本次分析" in source
    assert 'listRadarTasks("history")' in source
    assert "历史任务" in source
    assert "setHistory(await" in source
