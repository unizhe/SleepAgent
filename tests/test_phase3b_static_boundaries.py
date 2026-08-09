from __future__ import annotations

import ast
from pathlib import Path

from sleepagent.product_device import RadarDashboardProjectionTool
from sleepagent.radar_agent.api.http import RadarTaskCreateRequest


ROOT = Path(__file__).parents[1]
PRODUCT_AGENT = ROOT / "sleepagent" / "radar_agent" / "product_agent"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_canonical_product_code_cannot_import_retired_agent_runtimes() -> None:
    forbidden = (
        "sleepagent.radar_agent.agents",
        "sleepagent.radar_agent.orchestrator",
        "sleepagent.radar_agent.dynamic",
    )
    violations: list[str] = []
    for path in PRODUCT_AGENT.rglob("*.py"):
        if "skill_methods" in path.parts:
            continue
        for module in _imports(path):
            if module.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} -> {module}")
    assert violations == []


def test_skill_methods_are_reference_only() -> None:
    violations: list[str] = []
    for path in (ROOT / "sleepagent").rglob("*.py"):
        if "skill_methods" in path.parts:
            continue
        for module in _imports(path):
            if module.startswith(
                "sleepagent.radar_agent.product_agent.skill_methods"
            ):
                violations.append(f"{path.relative_to(ROOT)} -> {module}")
    assert violations == []


def test_executable_surfaces_have_no_retired_runtime_calls() -> None:
    surfaces = (
        ROOT / "sleepagent" / "radar_agent" / "api" / "http.py",
        ROOT / "sleepagent" / "radar_agent" / "cli" / "main.py",
        ROOT / "backend" / "main.py",
        ROOT / "sleepagent" / "product_device" / "api.py",
    )
    forbidden_tokens = (
        "DynamicTaskWorker",
        "DynamicOrchestratorRuntime",
        "RadarLangGraphWorkflow",
        "RadarProductAgent(",
        "ProductDialogueAgent(",
        "DeviceCareAgent",
        "RadarSleepAgentService(",
        ".worker.run_once(",
    )
    violations = [
        f"{path.relative_to(ROOT)}: {token}"
        for path in surfaces
        for token in forbidden_tokens
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_only_canonical_runtime_is_creatable() -> None:
    runtime_field = RadarTaskCreateRequest.model_fields["runtime_kind"]
    assert runtime_field.default == "product_episode"
    assert "product_episode" in str(runtime_field.annotation)
    assert "legacy_fixed" not in str(runtime_field.annotation)
    assert "dynamic_goal" not in str(runtime_field.annotation)
    assert RadarDashboardProjectionTool.__name__ == "RadarDashboardProjectionTool"


def test_retired_dynamic_acceptance_has_no_console_entrypoint() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dynamic-agent-acceptance" not in project


def test_product_device_root_exports_no_retired_agent_identity() -> None:
    import sleepagent.product_device as product_device

    for name in (
        "RadarProductAgent",
        "ProductDialogueAgent",
        "DeviceCareAgent",
        "RadarSleepAgentService",
    ):
        assert not hasattr(product_device, name)
