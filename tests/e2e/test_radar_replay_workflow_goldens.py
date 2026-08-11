from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from sleepagent.product_api.diagnostics.http import RadarApiRuntime
from sleepagent.product_runtime.cli import EXIT_OK, main as cli_main
from sleepagent.persistence import RadarPersistenceStore
from sleepagent.product_runtime.runtime_factory import (
    build_product_runtime_bundle_from_env,
)
from sleepagent.simulation.replay import (
    ReplayWorkflowGolden,
    get_replay_scenario,
    load_workflow_goldens,
    replay_scenario_ids,
)


GOLDENS = load_workflow_goldens()


def test_workflow_golden_catalog_is_fixed_and_covers_exactly_seven_scenarios() -> None:
    assert GOLDENS.version == "radar-workflow-goldens.v1"
    assert [item.scenario_id for item in GOLDENS.scenarios] == list(
        replay_scenario_ids()
    )
    assert len(GOLDENS.scenarios) == 7


@pytest.mark.parametrize("golden", GOLDENS.scenarios, ids=lambda item: item.scenario_id)
def test_retired_workflow_goldens_remain_consistent_replay_reference(
    golden: ReplayWorkflowGolden,
) -> None:
    """The old Workflow oracle is data-only after the runtime cutover."""

    scenario = get_replay_scenario(golden.scenario_id)
    assert scenario.expected.risk_level.value == golden.risk_level
    assert scenario.expected.questionnaire_candidates == golden.questionnaire_ids
    assert scenario.expected.confirmation_candidates == golden.confirmation_actions


def test_frontend_reads_runtime_questionnaires_instead_of_expected_catalog() -> None:
    project_root = Path(__file__).resolve().parents[2]
    workspace = (
        project_root / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text(encoding="utf-8")

    assert "detail.questionnaire_candidates" in workspace
    assert "scenario.expected.questionnaire_candidates" not in workspace


@pytest.mark.parametrize("golden", GOLDENS.scenarios, ids=lambda item: item.scenario_id)
def test_each_replay_demo_cli_uses_canonical_product_episode(
    golden: ReplayWorkflowGolden,
) -> None:
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    runtime = RadarApiRuntime(
        product_runtime=build_product_runtime_bundle_from_env(
            persistence_store=persistence,
        )
    )
    stdout = io.StringIO()

    assert cli_main(
        ["run-demo", "--scenario", golden.scenario_id, "--format", "jsonl"],
        runtime=runtime,
        stdout=stdout,
    ) == EXIT_OK

    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    task = runtime.service.get_task(rows[0]["task_id"])
    row_types = {row["type"] for row in rows}
    assert rows[0]["type"] == "trace"
    assert rows[0]["status"] == "completed"
    assert task.runtime_kind == "product_episode"
    assert task.runtime_contract_version == "product-episode.v1"
    assert {"trace", "event", "artifact"} <= row_types
    assert not any(item.startswith("dynamic_") for item in row_types)
