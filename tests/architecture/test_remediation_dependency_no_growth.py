from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
PACKAGE_ROOT = ROOT / "sleepagent"
BASELINE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "remediation"
    / "architecture_import_baseline.json"
)


def _module_name(path: Path) -> tuple[str, bool]:
    relative = path.relative_to(ROOT).with_suffix("")
    is_package = relative.name == "__init__"
    parts = relative.parts[:-1] if is_package else relative.parts
    return ".".join(parts), is_package


def _resolve_from_import(
    source: str,
    *,
    source_is_package: bool,
    module: str | None,
    level: int,
) -> str | None:
    if level == 0:
        return module
    package = source if source_is_package else source.rpartition(".")[0]
    parts = package.split(".") if package else []
    keep = len(parts) - (level - 1)
    if keep < 0:
        return None
    base = parts[:keep]
    if module:
        base.extend(module.split("."))
    return ".".join(base) or None


def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    known_modules = {_module_name(path)[0] for path in paths}
    for path in paths:
        source, source_is_package = _module_name(path)
        graph[source] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_from_import(
                    source,
                    source_is_package=source_is_package,
                    module=node.module,
                    level=node.level,
                )
                if resolved:
                    submodules = [
                        f"{resolved}.{alias.name}"
                        for alias in node.names
                        if f"{resolved}.{alias.name}" in known_modules
                    ]
                    targets.extend(submodules or [resolved])
            for target in targets:
                if target in known_modules:
                    graph[source].add(target)
    return graph


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(components))


def _forbidden_edges(graph: dict[str, set[str]]) -> tuple[str, ...]:
    violations: set[str] = set()
    for source, targets in graph.items():
        for target in targets:
            if source.startswith("sleepagent.domain.") and target.startswith(
                (
                    "sleepagent.runtime",
                    "sleepagent.workers",
                    "sleepagent.infrastructure",
                )
            ):
                violations.add(f"{source} -> {target}")
            if source in {
                "sleepagent.workers.kernel",
                "sleepagent.workers.runtime",
            } and target.startswith("sleepagent.workers."):
                violations.add(f"{source} -> {target}")
            if source == "sleepagent.process" and target == "sleepagent.app":
                violations.add(f"{source} -> {target}")
    return tuple(sorted(violations))


def _snapshot(graph: dict[str, set[str]] | None = None) -> dict[str, object]:
    current = _import_graph() if graph is None else graph
    return {
        "known_sccs": [list(item) for item in _strongly_connected_components(current)],
        "self_imports": sorted(
            f"{module} -> {module}"
            for module, targets in current.items()
            if module in targets
        ),
        "forbidden_edges": list(_forbidden_edges(current)),
    }


def _new_debt(
    current: dict[str, object],
    baseline: dict[str, object],
) -> dict[str, list[object]]:
    baseline_sccs = [
        frozenset(item) for item in baseline.get("known_sccs", [])
    ]
    current_sccs = [
        tuple(item) for item in current.get("known_sccs", [])
    ]
    return {
        "known_sccs": [
            item
            for item in current_sccs
            if not any(set(item) <= allowed for allowed in baseline_sccs)
        ],
        "self_imports": sorted(
            set(current.get("self_imports", []))
            - set(baseline.get("self_imports", []))
        ),
        "forbidden_edges": sorted(
            set(current.get("forbidden_edges", []))
            - set(baseline.get("forbidden_edges", []))
        ),
    }


def _baseline() -> dict[str, object]:
    value = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert value["schema_version"] == "architecture_import_baseline.g1.v1"
    return value


def test_import_architecture_does_not_grow_beyond_g1_baseline() -> None:
    debt = _new_debt(_snapshot(), _baseline())

    assert debt == {
        "known_sccs": [],
        "self_imports": [],
        "forbidden_edges": [],
    }


def test_guard_rejects_synthetic_new_forbidden_edge_and_cycle() -> None:
    current = _snapshot()
    current["forbidden_edges"] = [
        *current["forbidden_edges"],
        "sleepagent.domain.synthetic -> sleepagent.workers.synthetic",
    ]
    current["known_sccs"] = [
        *current["known_sccs"],
        ["sleepagent.synthetic.a", "sleepagent.synthetic.b"],
    ]

    debt = _new_debt(current, _baseline())

    assert debt["forbidden_edges"] == [
        "sleepagent.domain.synthetic -> sleepagent.workers.synthetic"
    ]
    assert debt["known_sccs"] == [
        ("sleepagent.synthetic.a", "sleepagent.synthetic.b")
    ]


def test_guard_allows_baseline_debt_to_shrink() -> None:
    baseline = _baseline()
    current = {
        "known_sccs": [],
        "self_imports": [],
        "forbidden_edges": [],
    }

    assert _new_debt(current, baseline) == {
        "known_sccs": [],
        "self_imports": [],
        "forbidden_edges": [],
    }
