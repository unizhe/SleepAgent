"""Machine-auditable report consumer inventory for the G4/G5 cutover."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Literal


ConsumerClassification = Literal[
    "LEGACY_ONLY",
    "COMPAT_ONLY",
    "DUAL_READ",
    "SHARED_READY",
    "RETIREMENT_UNKNOWN",
    "RETIRED",
]


_READ_CONSUMERS: tuple[tuple[str, ConsumerClassification, str], ...] = (
    ("product_report_api", "SHARED_READY", "sleepagent/api/postgres.py"),
    ("product_today", "SHARED_READY", "sleepagent/api/postgres.py"),
    ("public_v1_role_view", "SHARED_READY", "sleepagent/api/public.py"),
    ("report_cli", "SHARED_READY", "sleepagent/report_cli.py"),
    ("demo_api", "SHARED_READY", "sleepagent/api/demo.py"),
    ("demo_cli", "SHARED_READY", "sleepagent/simulation/cli.py"),
    ("reference_client", "SHARED_READY", "reference_client/sleep_api_v1_client.py"),
    ("historical_role_view_sql", "COMPAT_ONLY", "sleepagent/persistence/migrations"),
)


def _call_sites(path: Path, method: str, *, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == method:
            sites.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return sorted(sites)


def _text_evidence(path: Path, needle: str, *, root: Path) -> list[str]:
    return [
        f"{path.relative_to(root).as_posix()}:{line_number}"
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if needle in line
    ]


def build_report_consumer_audit(repository_root: Path) -> dict[str, Any]:
    root = repository_root.resolve()
    product_worker = root / "sleepagent/workers/product.py"
    configuration = root / "sleepagent/config.py"
    ingestion_worker = root / "sleepagent/workers/ingestion.py"
    fast_path = root / "sleepagent/infrastructure/postgres_sleep_slice.py"
    product_api = root / "sleepagent/api/postgres.py"
    report_cli = root / "sleepagent/report_cli.py"
    demo_cli = root / "sleepagent/simulation/cli.py"
    reference_client = root / "reference_client/sleep_api_v1_client.py"
    migration_dir = root / "sleepagent/persistence/migrations"

    read_consumers = [
        {
            "consumer": consumer,
            "classification": classification,
            "path": path,
        }
        for consumer, classification, path in _READ_CONSUMERS
    ]
    migration_files = sorted(migration_dir.glob("*.sql"))
    sql_legacy_read_evidence = sorted(
        evidence
        for path in migration_files
        for evidence in _text_evidence(path, "analysis_role_views", root=root)
    )
    return {
        "schema_version": "report_consumer_audit.v1",
        "future_reporting_authority": "shared_night_analysis",
        "read_consumers": read_consumers,
        "write_path_evidence": {
            "shared_prepare_call_sites": _call_sites(
                product_worker, "prepare_shared", root=root
            ),
            "legacy_prepare_call_sites": _call_sites(
                product_worker, "prepare", root=root
            ),
            "shared_operation_routes": _text_evidence(
                product_worker,
                "PRODUCT_SHARED_ANALYSIS_OPERATION",
                root=root,
            ),
            "compatibility_operation_creators": _text_evidence(
                fast_path,
                "product_agent_compatibility.v1",
                root=root,
            ),
            "shadow_comparison_sites": _text_evidence(
                product_worker,
                "build_report_shadow_comparison",
                root=root,
            ),
        },
        "read_path_evidence": {
            "product_api_shared_joins": _text_evidence(
                product_api,
                "product.shared_analysis.v1",
                root=root,
            ),
            "product_api_projection_reads": _text_evidence(
                product_api,
                "role_projection.v1",
                root=root,
            ),
            "report_cli_product_routes": _text_evidence(
                report_cli,
                "/product/sleep/",
                root=root,
            ),
            "demo_cli_projection_reads": _text_evidence(
                demo_cli,
                "role_projection",
                root=root,
            ),
            "reference_client_product_routes": _text_evidence(
                reference_client,
                "/product/sleep/",
                root=root,
            ),
            "historical_sql_role_view_reads": sql_legacy_read_evidence,
        },
        "cutover_evidence": {
            "shared_only_defaults": _text_evidence(
                configuration,
                "ReportPipelineMode.SHARED_ONLY",
                root=root,
            ),
            "compatibility_switch_wiring": _text_evidence(
                ingestion_worker,
                "emit_legacy_report_compatibility",
                root=root,
            ),
            "legacy_execution_guard": _text_evidence(
                product_worker,
                "legacy_report_write_path_retired",
                root=root,
            ),
        },
        "consumer_zero": {
            "new_legacy_per_role_operation_creation": False,
            "default_legacy_prepare_execution": False,
            "default_compatibility_bridge_write": False,
            "rollback_legacy_prepare_implementation_retained": True,
            "rollback_compatibility_bridge_implementation_retained": bool(
                _text_evidence(
                    fast_path, "product_agent_compatibility.v1", root=root
                )
            ),
            "historical_compatibility_reads_retained": bool(
                sql_legacy_read_evidence
            ),
        },
        "required_runtime_proofs": [
            "shadow comparison persists with authoritative_path=shared",
            "shadow comparison permits no external side effects",
            "one shared analysis creates exactly three deterministic projections",
            "product API resolves new reports through product.shared_analysis.v1",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    arguments = parser.parse_args(argv)
    print(
        json.dumps(
            build_report_consumer_audit(arguments.repository_root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
