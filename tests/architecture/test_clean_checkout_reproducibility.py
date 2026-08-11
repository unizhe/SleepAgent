from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
PYTHON_SOURCE_ROOTS = (
    ROOT / "backend",
    ROOT / "reference_client",
    ROOT / "sleepagent",
    ROOT / "tests",
)
LOCAL_ACCEPTANCE_ARCHIVE = re.compile(r"^sleepagent-v\d[\w.-]*\.zip$")


def _python_files() -> tuple[Path, ...]:
    return tuple(
        path
        for source_root in PYTHON_SOURCE_ROOTS
        for path in source_root.rglob("*.py")
    )


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_ignored_root_server_prototype_is_not_a_source_dependency() -> None:
    ignore_rules = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in _python_files()
        for module in _imported_modules(path)
        if module.split(".", maxsplit=1)[0] == "server"
    ]

    assert "server.py" in ignore_rules
    assert {"/build/", "/dist/"} <= ignore_rules
    assert violations == []


def test_tests_do_not_name_local_acceptance_zip_artifacts() -> None:
    violations: list[str] = []
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and LOCAL_ACCEPTANCE_ARCHIVE.fullmatch(node.value)
            ):
                violations.append(f"{path.relative_to(ROOT)} -> {node.value}")

    assert violations == []


def test_simulation_inventories_describe_generated_archives() -> None:
    for version in ("v18", "v24"):
        inventory = json.loads(
            (
                ROOT
                / "docs" / "product" / "habit-profile"
                / f"SIMULATION-COVERAGE-{version}.json"
            ).read_text(encoding="utf-8")
        )
        archive = inventory["archive"]

        assert "path" not in archive
        assert archive["distribution"] == "generated_not_committed"
        assert archive["archive_root"]
        assert not archive["archive_root"].endswith(".zip")
