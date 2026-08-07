"""Replay-only canonical data generation.

This package may construct synthetic external-world facts.  It is not an
alternate risk, readiness, Agent, or Memory implementation.
"""

from sleepagent.simulation.contracts import (
    ComponentVersionPin,
    CorrectionOverlay,
    GeneratedCorrectionLineage,
    GeneratedReplay,
    GeneratedReplayManifest,
    LateReportOverlay,
    LowCoverageOverlay,
    MissingMetricOverlay,
    NightRecipe,
    OfflineOverlay,
    ReplayEnvironment,
    ReplayScenario,
    ScenarioOverlay,
    ScenarioSeedRequest,
    SyntheticActor,
    SyntheticAuthorization,
    SyntheticIdentity,
)
from sleepagent.simulation.generator import (
    CanonicalReplayGenerator,
    ReplayFixtureError,
    canonical_replay_source_bytes,
    load_packaged_scenario,
    load_replay_scenario,
)

__all__ = [
    "CanonicalReplayGenerator",
    "ComponentVersionPin",
    "CorrectionOverlay",
    "GeneratedCorrectionLineage",
    "GeneratedReplay",
    "GeneratedReplayManifest",
    "LateReportOverlay",
    "LowCoverageOverlay",
    "MissingMetricOverlay",
    "NightRecipe",
    "OfflineOverlay",
    "ReplayEnvironment",
    "ReplayFixtureError",
    "ReplayScenario",
    "ScenarioOverlay",
    "ScenarioSeedRequest",
    "SyntheticActor",
    "SyntheticAuthorization",
    "SyntheticIdentity",
    "canonical_replay_source_bytes",
    "load_packaged_scenario",
    "load_replay_scenario",
]
