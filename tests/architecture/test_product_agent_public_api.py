from __future__ import annotations

import ast
import importlib
from pathlib import Path

import sleepagent.product_runtime as product_agent
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


REPOSITORY = Path(__file__).parents[2]
PACKAGE_ROOT = (
    REPOSITORY / "sleepagent/product_runtime/__init__.py"
)
PUBLIC_API = (
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
)
PUBLIC_OBJECTS = {
    "AgentId": AgentId,
    "AuthenticatedBinding": AuthenticatedBinding,
    "CareStrategyAgent": CareStrategyAgent,
    "CommitFrozenConfirmedAction": CommitFrozenConfirmedAction,
    "EpisodeReceipt": EpisodeReceipt,
    "EpisodeStatus": EpisodeStatus,
    "EpisodeType": EpisodeType,
    "EvidenceReasoningAgent": EvidenceReasoningAgent,
    "ExecutionMode": ExecutionMode,
    "ExternalActionTarget": ExternalActionTarget,
    "FactSnapshot": FactSnapshot,
    "PendingConfirmationTarget": PendingConfirmationTarget,
    "PendingUserInputTarget": PendingUserInputTarget,
    "PRODUCT_AGENT_ROSTER": PRODUCT_AGENT_ROSTER,
    "ProductAgentFactory": ProductAgentFactory,
    "ProductAgentRoster": ProductAgentRoster,
    "ProductEpisodeRunRequest": ProductEpisodeRunRequest,
    "ProductEpisodeRunResult": ProductEpisodeRunResult,
    "ProductEpisodeRunner": ProductEpisodeRunner,
    "ProductUserFactResponse": ProductUserFactResponse,
    "ReexecuteWithAddedFact": ReexecuteWithAddedFact,
    "SafetyReviewAgent": SafetyReviewAgent,
    "SleepCareAgent": SleepCareAgent,
    "SourceScope": SourceScope,
    "SourceScopeKind": SourceScopeKind,
}
FORBIDDEN_INTERNALS = {
    "ConfiguredExternalActionExecutor",
    "DeterministicCommitController",
    "HabitProfileApplicationService",
    "HumanDecisionRepository",
    "HumanDecisionService",
    "InMemoryCareContextStore",
    "InMemoryEpisodeCheckpointStore",
    "InMemoryHabitProfileStore",
    "InMemoryMemoryContextStore",
    "InMemoryProductEpisodeResultStore",
    "LegacyMemoryItemV1",
    "LongitudinalMemoryService",
    "PersistentCareContextStore",
    "PersistentCommitJournal",
    "PersistentHabitProfileStore",
    "PersistentHabitQuestionnaireStateStore",
    "PersistentHumanDecisionRepository",
    "PersistentMemoryContextStore",
    "PersistentProductEpisodeResultStore",
    "ProductEpisodeResultStore",
    "ProductEpisodeRunnerPort",
    "ProductEpisodeRuntime",
    "ProductInductionScheduler",
    "ProductRuntimeBundle",
    "ProductToolExecutor",
    "SkillRegistry",
}


def _reloaded_public_api():
    return importlib.reload(product_agent)


def test_public_namespace_is_the_exact_intentional_allowlist() -> None:
    module = _reloaded_public_api()

    assert tuple(module.__all__) == PUBLIC_API
    assert {
        name for name in vars(module) if not name.startswith("_")
    } == set(PUBLIC_API)
    assert all(getattr(module, name) is value for name, value in PUBLIC_OBJECTS.items())
    assert all(not hasattr(module, name) for name in FORBIDDEN_INTERNALS)


def test_star_import_exposes_only_the_intentional_allowlist() -> None:
    _reloaded_public_api()
    namespace: dict[str, object] = {}
    exec(
        "from sleepagent.product_runtime import *",
        namespace,
    )

    assert set(namespace).difference({"__builtins__"}) == set(PUBLIC_API)


def test_package_root_uses_only_explicit_owner_imports() -> None:
    tree = ast.parse(PACKAGE_ROOT.read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]

    assert all(alias.name != "*" for node in imports for alias in node.names)
    assert {node.module for node in imports} == {
        "sleepagent.product_runtime.agents",
        "sleepagent.product_runtime.contracts",
        "sleepagent.product_runtime.runner",
        "sleepagent.product_runtime.runtime_contracts",
    }


def test_repository_code_does_not_import_from_the_package_root() -> None:
    violations: list[str] = []
    for path in REPOSITORY.rglob("*.py"):
        if any(
            part in {".agents", ".git", "HealthClaw_paper", "__pycache__"}
            for part in path.parts
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "sleepagent.product_runtime"
            ):
                violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}")

    assert violations == []
