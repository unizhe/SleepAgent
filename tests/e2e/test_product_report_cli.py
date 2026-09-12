from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import pytest


pytestmark = pytest.mark.e2e


_PRIVATE_KEYS = {
    "episode_id",
    "episode_revision_id",
    "analysis_revision_id",
    "operation_id",
    "projection_id",
    "subject_id",
    "actor_id",
    "provider_request_id",
    "provider_request_ids",
}


def _configured_environment() -> tuple[dict[str, str], str]:
    if os.environ.get("SLEEPAGENT_E2E_REPORT_ENABLED") != "1":
        pytest.skip("set SLEEPAGENT_E2E_REPORT_ENABLED=1 for the report CLI E2E")
    required = (
        "SLEEPAGENT_REPORT_BASE_URL",
        "SLEEPAGENT_REPORT_SERVICE_CREDENTIAL",
        "SLEEPAGENT_REPORT_ACTOR_PRIVATE_KEY",
        "SLEEPAGENT_REPORT_ACTOR_ID",
        "SLEEPAGENT_REPORT_SUBJECT_ID",
        "SLEEPAGENT_REPORT_ROLE",
        "SLEEPAGENT_E2E_REPORT_WAKE_DATE",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        pytest.skip("report CLI E2E environment is incomplete")
    environment = dict(os.environ)
    wake_date = environment.pop("SLEEPAGENT_E2E_REPORT_WAKE_DATE")
    return environment, wake_date


def _run_cli(
    environment: Mapping[str, str],
    arguments: Sequence[str],
    *,
    allowed_exit_codes: frozenset[int] = frozenset({0}),
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "sleepagent.report_cli", *arguments],
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode in allowed_exit_codes, completed.stderr
    assert not completed.stderr
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    _assert_no_private_identifiers(value)
    return value


def _assert_no_private_identifiers(value: Any) -> None:
    if isinstance(value, Mapping):
        assert not (_PRIVATE_KEYS & set(value))
        for item in value.values():
            _assert_no_private_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_private_identifiers(item)


def test_cli_crosses_the_real_https_api_and_worker_process_boundary() -> None:
    """Opt-in proof against independently running HTTPS API/worker processes."""

    environment, wake_date = _configured_environment()

    accepted = _run_cli(
        environment,
        ["run", "--wake-date", wake_date, "--no-wait", "--json"],
    )
    assert accepted == {
        "schema_version": "product_sleep_report_run_accepted.v1",
        "state": "accepted",
        "status_url": f"/product/sleep/reports/{wake_date}",
        "wake_date": wake_date,
    }

    shown = _run_cli(
        environment,
        ["show", "--wake-date", wake_date, "--json", "--trace"],
        allowed_exit_codes=frozenset({0, 4}),
    )
    assert shown["schema_version"] == "product_sleep_report.v1"
    assert shown["wake_date"] == wake_date
    assert shown["state"] in {
        "pending",
        "ready",
        "failed",
        "stale",
        "policy_blocked",
        "unusable_blocked",
        "urgent_handled",
    }
    assert set(shown["trace"]) == {
        "elder_narrative",
        "fallback_used",
        "gate",
        "provider_call_count",
        "provider_input_tokens",
        "provider_output_tokens",
        "provider_request_ids_present",
        "shared_analysis",
    }

    listed = _run_cli(
        environment,
        ["list", "--limit", "100", "--json"],
    )
    assert listed["schema_version"] == "product_sleep_report_list.v1"
    assert any(item["wake_date"] == wake_date for item in listed["items"])
