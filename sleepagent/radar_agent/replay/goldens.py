from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field

from .scenarios import ReplayScenarioSchema


WORKFLOW_GOLDENS_PATH = Path(__file__).with_name("workflow_goldens.json")


class ReplayWorkflowGolden(ReplayScenarioSchema):
    scenario_id: str = Field(..., min_length=1)
    canonical_data_quality: str = Field(..., min_length=1)
    risk_level: str = Field(..., min_length=1)
    review_status: str = Field(..., min_length=1)
    claim_count: int = Field(..., ge=1)
    questionnaire_ids: list[str] = Field(default_factory=list)
    confirmation_actions: list[str] = Field(default_factory=list)
    automatic_actions: list[str] = Field(default_factory=list)
    memory_candidate_count: int = Field(default=0, ge=0)
    memory_rejected_inputs: list[str] = Field(default_factory=list)
    conflict_count: int = Field(default=0, ge=0)


class ReplayWorkflowCommonGolden(ReplayScenarioSchema):
    terminal_node: str = Field(..., min_length=1)
    agent_names: list[str] = Field(..., min_length=1)
    a2a_intents: list[str] = Field(..., min_length=1)
    report_roles: list[str] = Field(..., min_length=1)
    report_generation_mode: str = Field(..., min_length=1)
    artifact_types: list[str] = Field(..., min_length=1)
    required_audit_actions: list[str] = Field(..., min_length=1)


class ReplayWorkflowGoldenCatalog(ReplayScenarioSchema):
    version: str = Field(..., min_length=1)
    scenarios: list[ReplayWorkflowGolden] = Field(..., min_length=1)
    common: ReplayWorkflowCommonGolden


@lru_cache(maxsize=1)
def load_workflow_goldens() -> ReplayWorkflowGoldenCatalog:
    return ReplayWorkflowGoldenCatalog.model_validate(
        json.loads(WORKFLOW_GOLDENS_PATH.read_text(encoding="utf-8"))
    )


def get_workflow_golden(scenario_id: str) -> ReplayWorkflowGolden:
    for item in load_workflow_goldens().scenarios:
        if item.scenario_id == scenario_id:
            return item
    raise KeyError(f"unknown replay workflow golden: {scenario_id}")


__all__ = [
    "ReplayWorkflowCommonGolden",
    "ReplayWorkflowGolden",
    "ReplayWorkflowGoldenCatalog",
    "WORKFLOW_GOLDENS_PATH",
    "get_workflow_golden",
    "load_workflow_goldens",
]
