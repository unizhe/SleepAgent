from __future__ import annotations

import ast
from pathlib import Path

from setuptools import find_packages

from sleepagent.radar_agent.product_agent.agents import ProductAgentFactory
from sleepagent.radar_agent.product_agent.contracts import AgentId


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "sleepagent"
RADAR_ROOT = PACKAGE_ROOT / "radar_agent"
PRODUCTION_ROOTS = (
    PACKAGE_ROOT,
    ROOT / "backend",
    ROOT / "reference_client",
)

RETIRED_PACKAGES = (
    "agents",
    "orchestrator",
    "dynamic",
    "a2a",
    "prompts",
    "skills",
    "tools",
    "evidence",
    "memory",
)
RETIRED_IDENTITIES = (
    "RadarDataAgent",
    "TrendAgent",
    "RiskSignalAgent",
    "RAGAgent",
    "ReportAgent",
    "DialogueAgent",
    "AlertCareAgent",
    "MemoryAgent",
    "FullOrchestratorAgent",
    "DynamicOrchestratorRuntime",
    "DeviceCareAgent",
    "ProductDialogueAgent",
    "RadarProductAgent",
)
RETIRED_RUNTIME_KINDS = ("legacy_fixed", "dynamic_goal")


def _production_python_files() -> tuple[Path, ...]:
    return tuple(
        path
        for root in PRODUCTION_ROOTS
        if root.exists()
        for path in root.rglob("*.py")
    )


def test_retired_agent_runtime_packages_are_physically_absent() -> None:
    assert [name for name in RETIRED_PACKAGES if (RADAR_ROOT / name).exists()] == []
    assert not (PACKAGE_ROOT / "product_device" / "agents.py").exists()
    assert not (PACKAGE_ROOT / "product_device" / "dialogue.py").exists()
    assert not (PACKAGE_ROOT / "product_device" / "radar_agent.py").exists()


def test_importable_production_python_has_no_retired_execution_identity() -> None:
    violations: list[str] = []
    for path in _production_python_files():
        source = path.read_text(encoding="utf-8")
        for token in (*RETIRED_IDENTITIES, *RETIRED_RUNTIME_KINDS):
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")
    assert violations == []


def test_no_production_import_can_reintroduce_a_retired_package() -> None:
    prefixes = tuple(
        f"sleepagent.radar_agent.{name}" for name in RETIRED_PACKAGES
    )
    violations: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        for module in imported:
            if module.startswith(prefixes):
                violations.append(f"{path.relative_to(ROOT)} -> {module}")
    assert violations == []


def test_only_concrete_four_role_agent_roster_is_constructible() -> None:
    assert tuple(AgentId) == (
        AgentId.SLEEP_CARE,
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SAFETY_REVIEW,
    )
    model = object()
    roster = ProductAgentFactory.create(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
    )
    assert tuple(roster.as_mapping()) == tuple(AgentId)
    assert tuple(type(agent).__name__ for agent in roster.as_mapping().values()) == (
        "SleepCareAgent",
        "EvidenceReasoningAgent",
        "CareStrategyAgent",
        "SafetyReviewAgent",
    )


def test_distribution_discovery_excludes_retired_runtime_packages() -> None:
    packages = set(find_packages(where=ROOT, include=("sleepagent*",)))
    assert not {
        f"sleepagent.radar_agent.{name}" for name in RETIRED_PACKAGES
    }.intersection(packages)
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "langgraph" not in project.lower()
