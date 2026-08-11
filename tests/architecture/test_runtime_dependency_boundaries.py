from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from setuptools import find_packages

from sleepagent.backend_runtime import RuntimeServices
from sleepagent.backend_settings import ApiSurface
from sleepagent.product_device import RadarDashboardProjectionTool
from sleepagent.product_api.diagnostics.http import RadarTaskCreateRequest


ROOT = Path(__file__).parents[2]
PRODUCT_AGENT = ROOT / "sleepagent" / "product_runtime"


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
        for module in _imports(path):
            if module.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} -> {module}")
    assert violations == []


def test_skill_shadow_source_is_absent_and_no_python_source_imports_it() -> None:
    shadow = PRODUCT_AGENT / "skill_methods"
    assert not any(shadow.glob("*.py"))

    violations: list[str] = []
    for source_root in (ROOT / "sleepagent", ROOT / "tests"):
        for path in source_root.rglob("*.py"):
            for module in _imports(path):
                if module.startswith(
                    "sleepagent.product_runtime.skill_methods"
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)} -> {module}"
                    )
    assert violations == []


def test_package_discovery_contains_no_skill_shadow() -> None:
    project = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    configuration = project["tool"]["setuptools"]["packages"]["find"]
    discovered = find_packages(
        where=str(ROOT),
        include=tuple(configuration.get("include", ("*",))),
        exclude=tuple(configuration.get("exclude", ())),
    )

    shadow = "sleepagent.product_runtime.skill_methods"
    assert not any(
        package == shadow or package.startswith(f"{shadow}.")
        for package in discovered
    )


def test_executable_composition_surfaces_have_no_retired_runtime_calls() -> None:
    surfaces = (
        ROOT / "sleepagent" / "product_api" / "diagnostics" / "http.py",
        ROOT / "sleepagent" / "product_runtime" / "cli" / "main.py",
        ROOT / "backend" / "main.py",
        ROOT / "sleepagent" / "backend_app.py",
        ROOT / "sleepagent" / "backend_runtime.py",
        ROOT / "sleepagent" / "worker_runtime.py",
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


def test_only_canonical_runtime_is_creatable_and_composable() -> None:
    runtime_field = RadarTaskCreateRequest.model_fields["runtime_kind"]
    assert runtime_field.default == "product_episode"
    assert "product_episode" in str(runtime_field.annotation)
    assert "legacy_fixed" not in str(runtime_field.annotation)
    assert "dynamic_goal" not in str(runtime_field.annotation)
    assert "LEGACY_RADAR" not in ApiSurface.__members__
    assert "legacy_surface_installer" not in RuntimeServices.__dataclass_fields__
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
