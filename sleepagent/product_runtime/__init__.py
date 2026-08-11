"""Intentional public API for the four-Agent Product runtime."""

from sleepagent.product_runtime.agents import (
    CareStrategyAgent,
    EvidenceReasoningAgent,
    ProductAgentFactory,
    ProductAgentRoster,
    SafetyReviewAgent,
    SleepCareAgent,
)
from sleepagent.product_runtime.contracts import (
    PRODUCT_AGENT_ROSTER,
    AgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    ExternalActionTarget,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.product_runtime.runner import ProductEpisodeRunner
from sleepagent.product_runtime.runtime_contracts import (
    CommitFrozenConfirmedAction,
    PendingConfirmationTarget,
    PendingUserInputTarget,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductUserFactResponse,
    ReexecuteWithAddedFact,
)


__all__ = [
    "AgentId",
    "AuthenticatedBinding",
    "CareStrategyAgent",
    "CommitFrozenConfirmedAction",
    "EpisodeReceipt",
    "EpisodeStatus",
    "EpisodeType",
    "EvidenceReasoningAgent",
    "ExecutionMode",
    "ExternalActionTarget",
    "FactSnapshot",
    "PendingConfirmationTarget",
    "PendingUserInputTarget",
    "PRODUCT_AGENT_ROSTER",
    "ProductAgentFactory",
    "ProductAgentRoster",
    "ProductEpisodeRunRequest",
    "ProductEpisodeRunResult",
    "ProductEpisodeRunner",
    "ProductUserFactResponse",
    "ReexecuteWithAddedFact",
    "SafetyReviewAgent",
    "SleepCareAgent",
    "SourceScope",
    "SourceScopeKind",
]


# Importing the Runner necessarily loads its implementation modules. Keep those
# modules discoverable by their full paths without turning them into root API.
for _name in tuple(globals()):
    if not _name.startswith("_") and _name not in __all__:
        del globals()[_name]
del _name
