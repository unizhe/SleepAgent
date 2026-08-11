from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, is_dataclass
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pytest

import sleepagent
import sleepagent.radar_agent.product_agent.deterministic_model as deterministic_model
import sleepagent.radar_agent.product_agent.habit_runtime as habit_runtime
import sleepagent.radar_agent.product_agent.product_persistence as product_persistence
import sleepagent.radar_agent.product_agent.runner as runner_module
import sleepagent.radar_agent.product_agent.runtime_contracts as runtime_contracts
import sleepagent.radar_agent.product_agent.runtime_factory as runtime_factory
import sleepagent.radar_agent.product_agent.runtime_ports as runtime_ports
import sleepagent.radar_agent.product_agent.tooling as tooling_module
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    PRODUCT_AGENT_ROSTER,
    ToolEffect,
)
from sleepagent.radar_agent.product_agent.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
    build_deterministic_replay_product_runner,
)
from sleepagent.radar_agent.product_agent.runner import ProductEpisodeRunner
from sleepagent.radar_agent.product_agent.registry import (
    COLLABORATION_ALLOWLIST,
    COMMIT_CONTROLLER_TOOLS,
    EPISODE_DEFINITIONS,
    RUNTIME_INTERACTION_TOOLS,
    TOOL_DEFINITIONS,
    TOOL_INVOCATION_ALLOWLIST,
    product_agent_manifest,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
)
from sleepagent.radar_agent.product_agent.runtime_factory import (
    ProductRuntimeBundle,
    ProductRuntimePolicies,
    ProductRuntimeStores,
    build_product_runtime_bundle,
)
from sleepagent.radar_agent.product_agent.skills import (
    SKILL_FOUNDATION_VERSION,
    default_agent_profiles,
    default_skill_packages,
)

from tests.test_product_agent_factory import build_roster


_DTO_NAMES = (
    "ProductUserFactResponse",
    "ProductEpisodeRunRequest",
    "PendingConfirmationTarget",
    "PendingUserInputTarget",
    "ProductEpisodeRunResult",
    "ReexecuteWithAddedFact",
    "CommitFrozenConfirmedAction",
)
_PORT_NAMES = (
    "PublicationPublisher",
    "PublicationJournalEntryPort",
    "ProductEpisodeResultStore",
    "ProductEpisodeRunnerPort",
    "ProductToolExecutionContext",
    "ProductToolExecutorPort",
    "ProductToolResult",
    "ExternalActionExecutor",
)

_REAL_TOOL_OWNER_INVENTORY = {
    "radar.get_night_evidence": "data",
    "radar.get_range_evidence": "data",
    "radar.assess_data_quality": "quality",
    "radar.get_device_status": "quality",
    "runtime.build_fact_snapshot": "runtime",
    "cold_start.evaluate": "runtime",
    "trend.calculate_metrics": "trend",
    "risk.classify_signal": "risk",
    "risk.match_urgent_boundary": "risk",
    "reasoning.resolve_event_context": "reasoning",
    "baseline.read": "trend",
    "knowledge.retrieve_reviewed": "knowledge",
    "care.read_state": "care_state",
    "care.read_catalog": "care_catalog",
    "care.read_constraints": "care_catalog",
    "questionnaire.select_profile": "questionnaire",
    "questionnaire.capture_profile": "questionnaire",
    "artifact.render": "artifact",
    "memory.read": "memory",
    "memory.resolve_source": "memory",
    "memory.review_candidates": "memory",
    "memory.prepare_candidate": "memory",
    "profile.read": "memory",
    "profile.build_change_set": "memory",
    "coordination.read_policy": "coordination",
    "device.read_delivery_policy": "device",
    "policy.read": "policy",
    "state.commit_care": "commit_controller",
    "state.commit_memory": "commit_controller",
    "state.commit_habit_profile": "commit_controller",
    "external.notify": "commit_controller",
    "external.share": "commit_controller",
    "external.export": "commit_controller",
}
_REMOVED_FAKE_TOOLS = frozenset(
    {
        "evidence.read_ledger",
        "care.read_feedback",
        "questionnaire.select",
        "artifact.read",
        "memory.compare",
        "coordination.read_schedule",
        "confirmation.validate",
        "state.commit_evidence",
    }
)
_TOOL_CONTRACT_V2_SKILLS = frozenset(
    {
        "plan_episode",
        "evaluate_work_product",
        "draft_user_material",
        "draft_doctor_material",
        "propose_memory_change",
        "select_memory_context",
        "interpret_scoped_evidence",
        "synthesize_evidence_conflict",
        "interpret_longitudinal_pattern",
        "propose_single_care_action",
        "assess_followup_outcome",
        "draft_coordination_candidate",
        "review_claim_and_boundary",
        "review_action_and_publication",
    }
)


def _module_tree(module: object) -> ast.Module:
    module_path = Path(inspect.getfile(module))
    return ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _imported_modules(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_runtime_dtos_and_ports_have_lower_layer_module_ownership() -> None:
    for name in _DTO_NAMES:
        contract = getattr(runtime_contracts, name)
        assert contract.__module__ == runtime_contracts.__name__

    for name in _PORT_NAMES:
        port = getattr(runtime_ports, name)
        assert port.__module__ == runtime_ports.__name__


def test_runner_defines_no_runtime_contracts_handlers_or_implicit_graph() -> None:
    tree = _module_tree(runner_module)
    forbidden_definitions = set(_DTO_NAMES) | set(_PORT_NAMES)
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    assert forbidden_definitions.isdisjoint(definitions)

    handler_registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_handler"
    ]
    assert handler_registrations == []

    runner_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductEpisodeRunner"
    )
    constructor = next(
        node
        for node in runner_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert constructor.args.defaults == []
    assert all(default is None for default in constructor.args.kw_defaults)
    assert constructor.args.vararg is None
    assert constructor.args.kwarg is None

    implicit_infrastructure = {
        "ProductAgentFactory",
        "ProductToolExecutor",
        "SkillRegistry",
        "SkillResolver",
        "PromptCompiler",
        "DeterministicCommitController",
        "HumanDecisionService",
        "LongitudinalMemoryService",
        "HabitProfileRuntimeService",
        "DeterministicInductionWorker",
        "InMemoryProductEpisodeResultStore",
        "InMemoryLongitudinalResultStore",
    }
    constructor_calls = {
        name
        for node in ast.walk(constructor)
        if isinstance(node, ast.Call) and (name := _call_name(node)) is not None
    }
    assert implicit_infrastructure.isdisjoint(constructor_calls)


def test_lower_runtime_modules_do_not_import_runner_or_tooling_upward() -> None:
    persistence_imports = _imported_modules(_module_tree(product_persistence))
    assert "sleepagent.radar_agent.product_agent.runner" not in persistence_imports
    assert not any(name.endswith(".runner") for name in persistence_imports)

    habit_imports = _imported_modules(_module_tree(habit_runtime))
    assert "sleepagent.radar_agent.product_agent.tooling" not in habit_imports
    assert not any(name.endswith(".tooling") for name in habit_imports)


def test_runtime_factory_is_the_only_product_runner_composition_root() -> None:
    package_root = Path(sleepagent.__file__).resolve().parent
    constructor_calls: list[tuple[Path, int]] = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constructor_calls.extend(
            (path.relative_to(package_root), node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == "ProductEpisodeRunner"
        )

    assert [path.as_posix() for path, _line in constructor_calls] == [
        "radar_agent/product_agent/runtime_factory.py"
    ]
    factory_tree = _module_tree(runtime_factory)
    bundle_builder = _function(factory_tree, "build_product_runtime_bundle")
    assert sum(
        isinstance(node, ast.Call)
        and _call_name(node) == "ProductEpisodeRunner"
        for node in ast.walk(bundle_builder)
    ) == 1


def test_runtime_bundle_is_frozen_slotted_and_shares_one_object_graph() -> None:
    bundle = build_product_runtime_bundle(agent_roster=build_roster())

    for value, expected_type in (
        (bundle, ProductRuntimeBundle),
        (bundle.stores, ProductRuntimeStores),
        (bundle.policies, ProductRuntimePolicies),
    ):
        assert type(value) is expected_type
        assert is_dataclass(value)
        assert not hasattr(value, "__dict__")

    with pytest.raises(FrozenInstanceError):
        bundle.runner = bundle.runner  # type: ignore[misc]

    assert tuple(bundle.roster.as_mapping()) == PRODUCT_AGENT_ROSTER
    assert tuple(agent.agent_id for agent in bundle.roster) == PRODUCT_AGENT_ROSTER
    assert len(bundle.roster.as_mapping()) == 4
    assert set(bundle.agent_profiles) == set(AgentId)

    runner = bundle.runner
    assert runner.agent_roster is bundle.roster
    assert runner.tool_executor is bundle.tool_executor
    assert runner.publisher is bundle.publisher
    assert runner.result_store is bundle.stores.episode_results
    assert runner.care_catalog is bundle.policies.care_catalog
    assert runner.habit_runtime is bundle.habit_runtime
    assert runner.skill_registry is bundle.skill_registry
    assert runner.skill_resolver is bundle.skill_resolver
    assert runner.prompt_compiler is bundle.prompt_compiler
    assert runner.agent_profiles is bundle.agent_profiles
    assert runner.commit_controller is bundle.commit_controller
    assert runner.human_decisions is bundle.human_decisions
    assert runner.longitudinal_memory is bundle.longitudinal_memory
    assert runner.induction_worker is bundle.induction_worker
    assert runner.external_executor is bundle.external_executor
    assert runner.fact_snapshot_revalidator is bundle.policies.fact_snapshot_revalidator
    assert runner.provider_input_budget is bundle.provider_input_ledger
    assert runner.agent_invocation_coordinator is bundle.agent_invocation_coordinator
    assert runner.tool_execution_coordinator is bundle.tool_execution_coordinator
    assert runner.confirmed_action_coordinator is bundle.confirmed_action_coordinator
    assert runner.publication_service is bundle.publication_service
    assert runner.episode_result_finalizer is bundle.episode_result_finalizer

    assert bundle.commit_controller.care_store is bundle.stores.care_context
    assert bundle.commit_controller.memory_store is bundle.stores.memory_context
    assert (
        bundle.commit_controller.habit_profile_store
        is bundle.stores.habit_profile
    )
    assert bundle.longitudinal_memory.repository is bundle.stores.episode_results
    assert bundle.longitudinal_memory.memory_store is bundle.stores.memory_context
    assert bundle.habit_runtime.store is bundle.stores.habit_profile
    assert bundle.habit_application.runtime is bundle.habit_runtime
    assert bundle.habit_application.human_decisions is bundle.human_decisions
    assert bundle.habit_application.commit_controller is bundle.commit_controller


def test_registered_read_tools_equal_exact_bundle_handler_owners() -> None:
    bundle = build_product_runtime_bundle(agent_roster=build_roster())
    expected_handlers = {
        **bundle.core_tool_service.handlers(),
        **bundle.habit_runtime.handlers(),
        "memory.read": bundle.longitudinal_memory.read,
        "memory.resolve_source": bundle.longitudinal_memory.resolve_source,
        "memory.review_candidates": (
            bundle.longitudinal_memory.review_pending_candidates
        ),
        "memory.prepare_candidate": (
            bundle.longitudinal_memory.prepare_pending_candidate
        ),
        "care.read_state": bundle.runtime_read_service.read_current_care_state,
        "care.read_catalog": bundle.runtime_read_service.read_care_catalog,
        "care.read_constraints": (
            bundle.runtime_read_service.read_care_constraints
        ),
        "device.read_delivery_policy": (
            bundle.runtime_read_service.read_delivery_policy
        ),
        "coordination.read_policy": (
            bundle.runtime_read_service.read_coordination_policy
        ),
        "policy.read": bundle.runtime_read_service.read_runtime_policy,
    }
    registered_reads = {
        name
        for name, definition in TOOL_DEFINITIONS.items()
        if definition.effect is ToolEffect.READ_ONLY
    }

    assert registered_reads | set(RUNTIME_INTERACTION_TOOLS) == (
        set(bundle.tool_executor.handlers) | {"cold_start.evaluate"}
    )
    assert bundle.tool_executor.handlers == expected_handlers
    assert all(
        getattr(handler, "__name__", "") != "_passthrough"
        for handler in bundle.tool_executor.handlers.values()
    )
    assert "_passthrough" not in Path(
        inspect.getfile(tooling_module)
    ).read_text(encoding="utf-8")


def test_registered_tools_equal_explicit_real_owner_inventory() -> None:
    assert {
        name: definition.owner for name, definition in TOOL_DEFINITIONS.items()
    } == _REAL_TOOL_OWNER_INVENTORY
    registered_reads = {
        name
        for name, definition in TOOL_DEFINITIONS.items()
        if definition.effect is ToolEffect.READ_ONLY
    }
    assert set(TOOL_DEFINITIONS) == (
        registered_reads
        | set(RUNTIME_INTERACTION_TOOLS)
        | set(COMMIT_CONTROLLER_TOOLS)
    )
    assert RUNTIME_INTERACTION_TOOLS == {
        "questionnaire.select_profile",
        "questionnaire.capture_profile",
    }
    assert all(
        TOOL_DEFINITIONS[name].effect is ToolEffect.STATE_WRITE
        and TOOL_DEFINITIONS[name].owner == "questionnaire"
        and not TOOL_DEFINITIONS[name].confirmation_required
        for name in RUNTIME_INTERACTION_TOOLS
    )
    assert all(
        RUNTIME_INTERACTION_TOOLS.isdisjoint(tool_names)
        for tool_names in TOOL_INVOCATION_ALLOWLIST.values()
    )
    assert COMMIT_CONTROLLER_TOOLS == {
        "state.commit_care",
        "state.commit_memory",
        "state.commit_habit_profile",
        "external.notify",
        "external.share",
        "external.export",
    }


def test_registry_manifest_content_binds_the_exact_tool_owner_contract() -> None:
    manifest = product_agent_manifest()

    assert manifest["tool_definitions"] == {
        name: {
            "effect": definition.effect.value,
            "owner": definition.owner,
            "confirmation_required": definition.confirmation_required,
            "version": definition.version,
        }
        for name, definition in TOOL_DEFINITIONS.items()
    }
    assert manifest["commit_controller_tools"] == sorted(COMMIT_CONTROLLER_TOOLS)
    assert manifest["runtime_interaction_tools"] == sorted(
        RUNTIME_INTERACTION_TOOLS
    )
    assert manifest["collaboration_allowlist"] == {
        f"{sender.value}->{receiver.value}": sorted(
            request_type.value for request_type in request_types
        )
        for (sender, receiver), request_types in COLLABORATION_ALLOWLIST.items()
    }


def test_phase_c_tool_scope_changes_receive_new_profile_and_skill_versions() -> None:
    assert SKILL_FOUNDATION_VERSION == "sleepagent-product-skill-foundation.v6"
    assert {
        profile.version for profile in default_agent_profiles().values()
    } == {"2.0.0"}

    versions = {
        package.skill_id: package.version for package in default_skill_packages()
    }
    assert {
        skill_id for skill_id, version in versions.items() if version == "2.0.0"
    } == _TOOL_CONTRACT_V2_SKILLS
    assert {
        version
        for skill_id, version in versions.items()
        if skill_id not in _TOOL_CONTRACT_V2_SKILLS
    } == {"1.0.0"}


def test_deleted_fake_tools_are_absent_from_every_registry_surface() -> None:
    agent_tools = set().union(*TOOL_INVOCATION_ALLOWLIST.values())
    skill_tools = {
        tool_name
        for package in default_skill_packages()
        for tool_name in package.allowed_tool_requests
    }
    episode_tools = {
        tool_name
        for definition in EPISODE_DEFINITIONS.values()
        for tool_name in (
            *definition.required_tools,
            *definition.allowed_tools,
        )
    }

    for surface in (
        set(TOOL_DEFINITIONS),
        set(COMMIT_CONTROLLER_TOOLS),
        agent_tools,
        skill_tools,
        episode_tools,
    ):
        assert _REMOVED_FAKE_TOOLS.isdisjoint(surface)


def test_tool_and_service_modules_do_not_import_concrete_agents() -> None:
    package_root = Path(sleepagent.__file__).resolve().parent
    product_root = package_root / "radar_agent" / "product_agent"
    capability_paths = {
        product_root / "tooling.py",
        product_root / "habit_runtime.py",
        product_root / "longitudinal_memory.py",
        *(product_root / "tools").rglob("*.py"),
        *(product_root / "services").rglob("*.py"),
    }

    for path in sorted(capability_paths):
        imports = _imported_modules(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        assert not any(
            module == "sleepagent.radar_agent.product_agent.agents"
            or module.startswith(
                "sleepagent.radar_agent.product_agent.agents."
            )
            for module in imports
        ), path.relative_to(package_root)


def test_deterministic_runner_builder_delegates_to_bundle_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    captured: dict[str, object] = {}

    def build_fake_bundle(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(runner=sentinel)

    monkeypatch.setattr(
        runtime_factory,
        "build_deterministic_product_runtime_bundle",
        build_fake_bundle,
    )
    assert build_deterministic_replay_product_runner() is sentinel
    assert set(captured) == {"model"}
    assert isinstance(captured["model"], DeterministicReplayStructuredAgentModel)

    function = _function(
        _module_tree(deterministic_model),
        "build_deterministic_replay_product_runner",
    )
    calls = {
        name
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and (name := _call_name(node)) is not None
    }
    assert "build_deterministic_product_runtime_bundle" in calls
    assert "ProductEpisodeRunner" not in calls
    assert "ProductAgentFactory" not in calls


def test_product_runner_run_signature_remains_one_request_one_result() -> None:
    signature = inspect.signature(ProductEpisodeRunner.run)
    assert tuple(signature.parameters) == ("self", "request")
    assert (
        signature.parameters["request"].kind
        is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert signature.parameters["request"].default is inspect.Parameter.empty
    hints = get_type_hints(ProductEpisodeRunner.run)
    assert hints["request"] is ProductEpisodeRunRequest
    assert hints["return"] is ProductEpisodeRunResult
