from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_production_does_not_import_retired_subsystems() -> None:
    forbidden = (
        "sleepagent.product_device",
        "sleepagent.integrations." + "perceptor",
        "sleepagent.integrations.llm",
        "sleepagent.product_api." + "diagnostics",
        "sleepagent.persistence.store",
        "sleepagent.persistence.models",
        "sleepagent.sleep_api.service",
        "sleepagent.domain.repository",
    )
    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in (ROOT / "sleepagent").rglob("*.py")
        for module in _imports(path)
        if module.startswith(forbidden)
    ]
    assert violations == []
