"""Verifier-owned loading for human actions and expected oracle documents."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from simulation_verifier.contracts import (
    ExpectedOracle,
    SimulationAction,
    SimulationActionTimeline,
)


class VerifierFixtureError(ValueError):
    pass


_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "scenarios"
_ACTION_ADAPTER = TypeAdapter(SimulationAction)


def _scenario_directory(scenario_id: str) -> Path:
    if not scenario_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in scenario_id
    ):
        raise VerifierFixtureError("scenario_id must be a lowercase slug")
    directory = _FIXTURE_ROOT / scenario_id
    if not directory.is_dir() or directory.is_symlink():
        raise VerifierFixtureError(f"unknown verifier scenario: {scenario_id}")
    return directory


def load_action_timeline(scenario_id: str) -> SimulationActionTimeline:
    path = _scenario_directory(scenario_id) / "actions.jsonl"
    try:
        lines = tuple(
            line for line in path.read_text(encoding="utf-8").splitlines() if line
        )
        actions = tuple(_ACTION_ADAPTER.validate_json(line) for line in lines)
        return SimulationActionTimeline(scenario_id=scenario_id, actions=actions)
    except (OSError, ValidationError) as exc:
        raise VerifierFixtureError(
            f"invalid action timeline for {scenario_id}: {exc}"
        ) from exc


def load_expected_oracle(scenario_id: str) -> ExpectedOracle:
    path = _scenario_directory(scenario_id) / "expected.json"
    try:
        oracle = ExpectedOracle.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise VerifierFixtureError(
            f"invalid expected oracle for {scenario_id}: {exc}"
        ) from exc
    if oracle.scenario_id != scenario_id:
        raise VerifierFixtureError("oracle scenario_id does not match its directory")
    return oracle


__all__ = [
    "VerifierFixtureError",
    "load_action_timeline",
    "load_expected_oracle",
]
