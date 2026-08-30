from __future__ import annotations

from pathlib import Path

import pytest

from sleepagent.report_consumer_audit import build_report_consumer_audit


pytestmark = pytest.mark.unit


def test_report_consumer_audit_combines_runtime_ast_api_cli_and_sql_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    audit = build_report_consumer_audit(root)

    assert audit["future_reporting_authority"] == "shared_night_analysis"
    assert {
        item["classification"] for item in audit["read_consumers"]
    } <= {"SHARED_READY", "COMPAT_ONLY"}
    assert audit["write_path_evidence"]["shared_prepare_call_sites"]
    assert audit["write_path_evidence"]["legacy_prepare_call_sites"]
    assert audit["write_path_evidence"]["shadow_comparison_sites"]
    assert audit["read_path_evidence"]["product_api_shared_joins"]
    assert audit["read_path_evidence"]["report_cli_product_routes"]
    assert audit["read_path_evidence"]["reference_client_product_routes"]
    assert audit["read_path_evidence"]["historical_sql_role_view_reads"]
    assert audit["consumer_zero"] == {
        "new_legacy_per_role_operation_creation": False,
        "legacy_prepare_implementation_retained": True,
        "compatibility_bridge_write_retained": True,
        "historical_compatibility_reads_retained": True,
    }
