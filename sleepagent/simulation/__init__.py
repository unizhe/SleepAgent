"""Replay-only canonical data generation.

This package may construct synthetic external-world facts.  It is not an
alternate risk, readiness, Agent, or Memory implementation.
"""

from typing import Any

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
    "ReplayExternalFactAdapter",
    "ReplayFixtureError",
    "ReplayIngressItem",
    "ReplayIngressManifest",
    "ReplayScenario",
    "ScenarioOverlay",
    "ScenarioSeedRequest",
    "SyntheticActor",
    "SyntheticAuthorization",
    "SyntheticIdentity",
    "VendorAlertOverlay",
    "canonical_replay_source_bytes",
    "load_packaged_scenario",
    "load_replay_scenario",
]


_INGRESS_EXPORTS = frozenset(
    {
        "ReplayExternalFactAdapter",
        "ReplayIngressItem",
        "ReplayIngressManifest",
    }
)

_CONTRACT_EXPORTS = frozenset(__all__) - _INGRESS_EXPORTS - {
    "CanonicalReplayGenerator",
    "ReplayFixtureError",
    "canonical_replay_source_bytes",
    "load_packaged_scenario",
    "load_replay_scenario",
}


def __getattr__(name: str) -> Any:
    """Load the canonical generator lazily so replay catalogs stay leaf modules."""

    if name in _CONTRACT_EXPORTS:
        from sleepagent.simulation import contracts

        return getattr(contracts, name)
    if name in _INGRESS_EXPORTS:
        from sleepagent.simulation import replay_ingress

        return getattr(replay_ingress, name)
    if name in set(__all__) - _CONTRACT_EXPORTS:
        from sleepagent.simulation import generator

        return getattr(generator, name)
    raise AttributeError(name)
