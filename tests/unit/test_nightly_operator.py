from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
NIGHTLY = REPOSITORY / "scripts" / "nightly.sh"
NIGHTLY_SQL = REPOSITORY / "scripts" / "nightly_readonly.sql"
ACCEPTANCE = REPOSITORY / "scripts" / "b_stabilization_readonly_acceptance.sh"
ACCEPTANCE_SQL = REPOSITORY / "scripts" / "b_stabilization_readonly_acceptance.sql"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _night_payload(wake_date: str) -> dict[str, object]:
    return {
        "schema_version": "sleepagent.nightly_summary.v1",
        "wake_date": wake_date,
        "night_found": True,
        "transaction_read_only": True,
        "acquisition": {
            "push": {"status": "stored", "raw_receipt_count": 12},
            "history": {"status": "normalized", "raw_response_count": 2},
            "sleep_report": {
                "status": "available",
                "raw_response_count": 1,
                "source_version_count": 1,
            },
        },
        "night_episode": {
            "episode_id": "episode-1",
            "current_revision": 4,
            "state": "awaiting_report",
            "collection_start_at": "2026-09-09T22:00:00+08:00",
            "bed_at": "2026-09-09T22:03:00+08:00",
            "wake_at": "2026-09-10T07:00:00+08:00",
            "boundary_policy_version": "boundary-v2-wake-confirmation",
        },
        "wake_confirmation": {
            "candidate_wake_at": None,
            "latest_bed_presence_at": "2026-09-10T07:02:00+08:00",
            "confirmed_revision": 3,
            "confirmed_revision_cause": "confirmed_observed_wake",
        },
        "data": {
            "canonical_observation_count": 12,
            "observation_type_counts": {"bed_presence": 4, "heart_rate": 8},
        },
        "finalization": {
            "state": "soft_finalized",
            "coverage_status": "partial",
            "current_revision": 1,
            "revision_cause": "wake_grace_elapsed",
        },
        "product": {"analysis_revision_count": 0, "operation_statuses": {}},
    }


def _operator_environment(
    tmp_path: Path, *, acceptance_exit: int = 0, night_found: bool = True
) -> dict[str, str]:
    report_env = tmp_path / "report.env"
    report_env.write_text("export SLEEPAGENT_ONE_NIGHT_READ_DSN=fake-read-dsn\n", encoding="utf-8")
    acceptance = tmp_path / "acceptance"
    _write_executable(
        acceptance,
        "#!/usr/bin/env bash\n"
        "printf 'acceptance for %s\\n' \"$1\" >\"$2\"\n"
        f"exit {acceptance_exit}\n",
    )
    fake_psql = tmp_path / "psql"
    summary_payload = _night_payload("__WAKE_DATE__")
    summary_payload["night_found"] = night_found
    if not night_found:
        summary_payload["night_episode"] = None
    summary = json.dumps(summary_payload, separators=(",", ":"))
    listing = json.dumps(
        {
            "schema_version": "sleepagent.nightly_list.v1",
            "transaction_read_only": True,
            "nights": [
                {
                    "wake_date": "2026-09-10",
                    "episode_state": "awaiting_report",
                    "wake": "07:00",
                    "finalization_state": "soft_finalized",
                    "report_state": "available",
                }
            ],
        },
        separators=(",", ":"),
    )
    _write_executable(
        fake_psql,
        "#!/usr/bin/env bash\n"
        "args=\"$*\"\n"
        f"list_json='{listing}'\n"
        f"summary_json='{summary}'\n"
        "if [[ \"$args\" == *'list_mode=1'* ]]; then\n"
        "  printf '%s\\n' \"$list_json\"\n"
        "else\n"
        "  wake_date=unknown\n"
        "  for argument in \"$@\"; do\n"
        "    if [[ \"$argument\" == wake_date=* ]]; then wake_date=${argument#wake_date=}; fi\n"
        "  done\n"
        "  printf '%s\\n' \"${summary_json/__WAKE_DATE__/$wake_date}\"\n"
        "fi\n",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "SLEEPAGENT_REPORT_ENV_FILE": str(report_env),
            "SLEEPAGENT_PSQL_BIN": str(fake_psql),
            "SLEEPAGENT_PYTHON_BIN": sys.executable,
            "SLEEPAGENT_NIGHTLY_ACCEPTANCE": str(acceptance),
            "SLEEPAGENT_NIGHTLY_ARCHIVE_ROOT": str(tmp_path / "nightly-runs"),
        }
    )
    return environment


def _run(
    tmp_path: Path,
    *arguments: str,
    acceptance_exit: int = 0,
    night_found: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(NIGHTLY), *arguments],
        cwd=REPOSITORY,
        env=_operator_environment(
            tmp_path, acceptance_exit=acceptance_exit, night_found=night_found
        ),
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_resolves_previous_asia_shanghai_date_and_archives(tmp_path: Path) -> None:
    environment = _operator_environment(tmp_path)
    environment["SLEEPAGENT_NIGHTLY_NOW"] = "2026-09-11T00:05:00+08:00"
    result = subprocess.run(
        [str(NIGHTLY)], cwd=REPOSITORY, env=environment,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Night: 2026-09-10" in result.stdout
    night_dir = tmp_path / "nightly-runs" / "2026-09-10"
    assert {path.name for path in night_dir.iterdir()} == {
        "acceptance.log", "summary.json", "summary.txt"
    }
    payload = json.loads((night_dir / "summary.json").read_text(encoding="utf-8"))
    assert payload["transaction_read_only"] is True
    assert payload["acceptance"] == {"exit_code": 0, "status": "passed"}


def test_explicit_date_and_repeat_replace_deterministic_artifacts(tmp_path: Path) -> None:
    first = _run(tmp_path, "2026-09-08")
    second = _run(tmp_path, "2026-09-08")
    assert first.returncode == second.returncode == 0
    night_dir = tmp_path / "nightly-runs" / "2026-09-08"
    assert {path.name for path in night_dir.iterdir()} == {
        "acceptance.log", "summary.json", "summary.txt"
    }
    assert "wake_date=2026-09-08" in second.stdout


def test_list_mode_uses_compact_recent_night_table(tmp_path: Path) -> None:
    result = _run(tmp_path, "list")
    assert result.returncode == 0, result.stderr
    assert "EPISODE_STATE" in result.stdout
    assert "2026-09-10" in result.stdout
    assert "soft_finalized" in result.stdout
    assert not (tmp_path / "nightly-runs").exists()


def test_invalid_date_is_rejected_before_acceptance(tmp_path: Path) -> None:
    result = _run(tmp_path, "2026-02-30")
    assert result.returncode == 2
    assert "Invalid wake date" in result.stderr
    assert not (tmp_path / "nightly-runs").exists()


def test_acceptance_failure_exit_code_is_preserved_with_summary(tmp_path: Path) -> None:
    result = _run(tmp_path, "2026-09-08", acceptance_exit=7)
    assert result.returncode == 7
    assert "Acceptance: FAILED (exit 7)" in result.stdout
    payload = json.loads(
        (tmp_path / "nightly-runs" / "2026-09-08" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["acceptance"] == {"exit_code": 7, "status": "failed"}


def test_missing_stored_night_is_explicit_and_does_not_start_backfill(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "2026-09-03", night_found=False)
    assert result.returncode == 3
    assert "NIGHT_NOT_FOUND=2026-09-03" in result.stdout
    assert "never started by nightly.sh" in result.stdout


def test_nightly_query_is_read_only_and_has_no_external_call_path() -> None:
    sql = NIGHTLY_SQL.read_text(encoding="utf-8")
    shell = NIGHTLY.read_text(encoding="utf-8")
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    acceptance_sql = ACCEPTANCE_SQL.read_text(encoding="utf-8")
    assert "BEGIN READ ONLY;" in sql
    assert "SET LOCAL statement_timeout = '30s';" in sql
    assert "current_setting('transaction_read_only')" in sql
    assert re.search(r"(?im)^\s*(INSERT|UPDATE|DELETE|MERGE|CALL)\b", sql) is None
    assert "BEGIN READ ONLY;" in acceptance_sql
    assert "SET LOCAL statement_timeout = '8s';" in acceptance_sql
    assert re.search(
        r"(?im)^\s*(INSERT|UPDATE|DELETE|MERGE|CALL)\b", acceptance_sql
    ) is None
    assert "scheduled_for < (:'wake_date'::date + 1)" in acceptance_sql
    assert "work.created_at < (:'wake_date'::date + 1)" in acceptance_sql
    assert "bounded_normalization_work AS MATERIALIZED" in acceptance_sql
    assert "bounded_pull_work AS MATERIALIZED" in acceptance_sql
    assert "work.created_at >= acceptance.starts_at" in acceptance_sql
    assert "work.created_at < acceptance.ends_at" in acceptance_sql
    assert "work.work_json ->> 'normalizer' = 'perceptor_pull'" in acceptance_sql
    assert "raw.namespace_id = work.namespace_id" in acceptance_sql
    assert "raw.data_mode = work.data_mode" in acceptance_sql
    assert "FROM bounded_normalization_work AS work" in acceptance_sql
    assert "The original effective-time predicate remains" in acceptance_sql
    assert "DeepSeek" not in shell
    assert "perceptor" not in shell.lower()
    assert "curl" not in shell
    assert "curl" not in acceptance
    assert "PerceptorPlatformClient" not in acceptance


def test_nightly_list_does_not_read_protected_source_reports() -> None:
    sql = NIGHTLY_SQL.read_text(encoding="utf-8")
    assert "sleep_domain_source_reports" not in sql
    assert "sleep_domain_episode_observation_memberships" not in sql
    assert "sleep_domain_canonical_observations" not in sql
    assert "finalization_revision.source_report_version_id" in sql


def test_nightly_sql_does_not_use_reserved_window_alias() -> None:
    sql = NIGHTLY_SQL.read_text(encoding="utf-8")
    assert re.search(r"(?i)\b(?:AS\s+)?window\b", sql) is None
    assert "night_window AS nw" in sql
