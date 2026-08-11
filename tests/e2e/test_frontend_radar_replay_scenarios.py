from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "sleepagent/simulation/replay/scenario_catalog.json"


def test_frontend_uses_shared_replay_catalog_for_demo_data() -> None:
    demo_source = (PROJECT_ROOT / "frontend/lib/radar-demo-data.ts").read_text()
    scenario_source = (PROJECT_ROOT / "frontend/lib/radar-replay-scenarios.ts").read_text()

    assert "scenario_catalog.json" in scenario_source
    assert "buildRadarWorkspaceDataFromScenario" in demo_source
    assert "current_snapshot" not in demo_source
    assert "sleep_score" not in demo_source
    assert "recent_alerts" not in demo_source


def test_frontend_workspace_exposes_catalog_driven_scenario_selector() -> None:
    workspace_source = (
        PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text()

    assert 'id="radar-scenario"' in workspace_source
    assert "radarReplayScenarios.map" in workspace_source
    assert "scenarioId" in workspace_source
    for scenario_id in _catalog_scenario_ids():
        assert scenario_id not in workspace_source
    assert "localizedReplayScenarioTitle" in workspace_source


def test_frontend_api_client_creates_and_runs_real_task_with_scenario() -> None:
    api_source = (PROJECT_ROOT / "frontend/lib/radar-api.ts").read_text()

    assert 'const RADAR_TASK_API_BASE = `${RADAR_API_PROXY_BASE}/radar-agent`' in api_source
    assert "createRadarTask" in api_source
    assert "runRadarTask" in api_source
    assert "scenario: scenarioId" in api_source
    assert 'new EventSource(' in api_source
    assert '"Idempotency-Key": idempotencyKey' in api_source
    workspace_source = (
        PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text()
    assert "detail?.replay_scenario ?? scenario" in workspace_source


def test_all_seven_catalog_states_have_dashboard_risk_and_quality_coverage() -> None:
    catalog = json.loads(CATALOG_PATH.read_text())
    workspace_source = (
        PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text()

    assert len(catalog["scenarios"]) == 7
    assert {item["expected"]["risk_level"] for item in catalog["scenarios"]} <= {
        "info",
        "uncertain",
        "watch",
        "escalate",
        "urgent_boundary",
    }
    for state in ("info", "uncertain", "watch", "escalate", "urgent_boundary"):
        assert state in workspace_source
    for state in ("good", "partial", "unusable", "urgent_text_override"):
        assert state in workspace_source


def test_workspace_has_three_role_views_and_keeps_chat_auxiliary() -> None:
    workspace_source = (
        PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text()

    for view in ("ElderView", "FamilyView", "DoctorView"):
        assert f"function {view}" in workspace_source
    assert "一个主要建议" in workspace_source
    assert "补充问卷" in workspace_source
    assert "结构化证据链" in workspace_source
    assert "确认并导出" in workspace_source
    assert 'item.action_type === "export_doctor_material"' in workspace_source
    assert '!["pending", "approved"].includes' in workspace_source
    assert "if (!approved) return" in workspace_source
    assert "ChatPanel" in workspace_source
    assert "多 Agent 分析过程" in workspace_source
    assert "Multi-Agent 过程" not in workspace_source
    assert "<details" in workspace_source


def test_workspace_visual_language_avoids_forbidden_dashboard_patterns() -> None:
    workspace_source = (
        PROJECT_ROOT / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text().lower()
    globals_source = (PROJECT_ROOT / "frontend/app/globals.css").read_text().lower()

    assert "purple" not in workspace_source
    assert "violet" not in workspace_source
    assert "backdrop-blur" not in workspace_source
    assert "linear-gradient" not in workspace_source
    assert "#f4f1e9" in workspace_source
    assert "linear-gradient" not in globals_source


def _catalog_scenario_ids() -> list[str]:
    payload = json.loads(CATALOG_PATH.read_text())
    return [item["scenario_id"] for item in payload["scenarios"]]
