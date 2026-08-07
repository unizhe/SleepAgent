from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import sleepagent.radar_agent.agents.report as report_caller
import sleepagent.radar_agent.reports as public_reports
import sleepagent.radar_agent.reports.roles as report_roles
from sleepagent.radar_agent.schemas import (
    RoleReportArtifact as CanonicalRoleReportArtifact,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXPORTS = [
    "REPORT_ROLE_ORDER",
    "ReportRole",
    "RoleReportArtifact",
    "RoleReportBundle",
]


def _is_git_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in {0, 1}
    return result.returncode == 0


def _active_ignore_rules(path: Path) -> list[str]:
    return [
        line
        for raw_line in path.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]


def test_only_root_report_artifacts_are_git_ignored() -> None:
    assert _is_git_ignored("reports/package-boundary-probe.json")
    assert not _is_git_ignored("sleepagent/radar_agent/reports/__init__.py")
    assert not _is_git_ignored("sleepagent/radar_agent/reports/rendering.py")
    assert not _is_git_ignored("sleepagent/radar_agent/reports/roles.py")


def test_docker_ignore_keeps_its_context_root_reports_rule() -> None:
    rules = _active_ignore_rules(ROOT / ".dockerignore")

    assert "reports/" in rules
    assert "**/reports/" not in rules
    assert "**/reports/**" not in rules


def test_reports_package_is_discoverable_for_distribution() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from setuptools import find_packages; "
            "print(json.dumps(find_packages(include=(\"sleepagent*\", "
            "\"reference_client*\"))))",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    packages = json.loads(result.stdout)

    assert "sleepagent.radar_agent.reports" in packages
    assert (ROOT / "sleepagent/radar_agent/reports/rendering.py").is_file()


def test_public_report_contract_and_direct_caller_are_unchanged() -> None:
    assert public_reports.__all__ == EXPECTED_EXPORTS
    assert report_roles.__all__ == EXPECTED_EXPORTS
    assert [role.value for role in public_reports.ReportRole] == [
        "elder",
        "family",
        "doctor",
    ]
    assert public_reports.REPORT_ROLE_ORDER == (
        public_reports.ReportRole.ELDER,
        public_reports.ReportRole.FAMILY,
        public_reports.ReportRole.DOCTOR,
    )
    assert public_reports.RoleReportArtifact is CanonicalRoleReportArtifact
    assert report_caller.REPORT_ROLE_ORDER is public_reports.REPORT_ROLE_ORDER
    assert report_caller.RoleReportBundle is public_reports.RoleReportBundle

    bundle = public_reports.RoleReportBundle(task_id="task-package-boundary")
    assert bundle.for_role(public_reports.ReportRole.ELDER) is None
    assert bundle.model_dump(mode="json") == {
        "standard_mappings": {
            "fhir_profile": None,
            "fhir_resource": None,
            "local_codes": {},
            "loinc_codes": [],
            "notes": [],
            "snomed_codes": [],
        },
        "task_id": "task-package-boundary",
        "reports": [],
    }
