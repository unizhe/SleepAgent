from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
import pytest

from tests.support.diagnostic_app import app
from sleepagent.product_api.diagnostics.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
)
from sleepagent.product_runtime.agent_invocation_coordinator import (
    AgentInvocationCoordinator,
)
from sleepagent.product_runtime.agents import (
    ProductAgentFactory,
    ProductAgentRoster,
)
from sleepagent.product_runtime.confirmed_action_coordinator import (
    ConfirmedActionCoordinator,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.product_runtime.episode_result_finalizer import (
    EpisodeResultFinalizer,
)
from sleepagent.product_runtime.publication_service import (
    PublicationService,
)
from sleepagent.product_runtime.runner import ProductEpisodeRunner
from sleepagent.product_runtime.runtime_contracts import (
    PendingUserInputTarget,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductUserFactResponse,
    ReexecuteWithAddedFact,
    bind_product_episode_checkpoint,
    product_episode_request_hash,
)
from sleepagent.product_runtime.runtime_factory import (
    build_product_runtime_bundle,
)
from sleepagent.product_runtime.skills import (
    SkillRegistry,
    default_skill_packages,
)
from sleepagent.product_runtime.tool_execution_coordinator import (
    ToolExecutionCoordinator,
)
from tests.integration.test_radar_task_api import (
    ACTOR_ID as API_ACTOR_ID,
    API_KEY,
    UserInputProductRunner,
    _product_runtime_with_runner,
)
from tests.support.runtime_fixtures import (
    configure_test_radar_runtime as reset_radar_api_runtime_for_tests,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_PACKAGE = PROJECT_ROOT / "sleepagent" / "product_runtime"
RUNTIME_FACTORY_PATH = PRODUCT_PACKAGE / "runtime_factory.py"
RUNNER_PATH = PRODUCT_PACKAGE / "runner.py"

COMPONENT_CONSTRUCTORS = (
    "ProductEpisodeRunner",
    "AgentInvocationCoordinator",
    "ToolExecutionCoordinator",
    "ConfirmedActionCoordinator",
    "PublicationService",
    "EpisodeResultFinalizer",
)
EXTRACTED_COMPONENT_PATHS = tuple(
    PRODUCT_PACKAGE / name
    for name in (
        "agent_invocation_coordinator.py",
        "tool_execution_coordinator.py",
        "confirmed_action_coordinator.py",
        "publication_service.py",
        "episode_result_finalizer.py",
        "runtime_contracts.py",
        "runtime_ports.py",
    )
)
CONCRETE_AGENT_NAMES = frozenset(
    {
        "SleepCareAgent",
        "EvidenceReasoningAgent",
        "CareStrategyAgent",
        "SafetyReviewAgent",
    }
)
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class _NeverCalledModel:
    provider = "test"
    model_id = "runner-contraction-never-called"

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        prompt_version: str,
        context_packet_id: str,
    ) -> SchemaT:
        raise AssertionError("composition tests must not invoke a model")


def _build_roster() -> ProductAgentRoster:
    model = _NeverCalledModel()
    registry = SkillRegistry(default_skill_packages())
    return ProductAgentFactory.create(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        skill_registry=registry,
    )


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _expression_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _attribute_path(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_attribute_path(node.value), node.attr)
    return ()


def _runner_class() -> ast.ClassDef:
    matches = [
        node
        for node in _parse(RUNNER_PATH).body
        if isinstance(node, ast.ClassDef) and node.name == "ProductEpisodeRunner"
    ]
    assert len(matches) == 1
    return matches[0]


def test_runtime_factory_wires_the_exact_five_contraction_components() -> None:
    bundle = build_product_runtime_bundle(agent_roster=_build_roster())
    components = (
        bundle.agent_invocation_coordinator,
        bundle.tool_execution_coordinator,
        bundle.confirmed_action_coordinator,
        bundle.publication_service,
        bundle.episode_result_finalizer,
    )

    assert tuple(type(item) for item in components) == (
        AgentInvocationCoordinator,
        ToolExecutionCoordinator,
        ConfirmedActionCoordinator,
        PublicationService,
        EpisodeResultFinalizer,
    )
    assert bundle.runner.agent_invocation_coordinator is components[0]
    assert bundle.runner.tool_execution_coordinator is components[1]
    assert bundle.runner.confirmed_action_coordinator is components[2]
    assert bundle.runner.publication_service is components[3]
    assert bundle.runner.episode_result_finalizer is components[4]
    assert components[0].tool_execution_coordinator is components[1]
    assert components[0].provider_input_budget is bundle.provider_input_ledger
    assert components[4].tool_execution_coordinator is components[1]
    assert components[4].provider_input_budget_ledger is bundle.provider_input_ledger


def test_runtime_factory_is_the_only_production_constructor_for_runner_and_components() -> None:
    construction_sites: dict[str, list[Path]] = {
        name: [] for name in COMPONENT_CONSTRUCTORS
    }
    source_roots = tuple(
        path
        for path in (
            PROJECT_ROOT / "sleepagent",
            PROJECT_ROOT / "backend",
            PROJECT_ROOT / "reference_client",
        )
        if path.exists()
    )
    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            tree = _parse(path)
            imported_names = {
                alias.asname or alias.name: alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                local_name = _expression_name(node.func)
                constructor = imported_names.get(local_name, local_name)
                if constructor in construction_sites:
                    construction_sites[constructor].append(path)

    expected = RUNTIME_FACTORY_PATH.resolve()
    assert {
        name: [path.resolve() for path in paths]
        for name, paths in construction_sites.items()
    } == {name: [expected] for name in COMPONENT_CONSTRUCTORS}

    builder = next(
        node
        for node in _parse(RUNTIME_FACTORY_PATH).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_product_runtime_bundle"
    )
    calls = [
        _expression_name(node.func)
        for node in ast.walk(builder)
        if isinstance(node, ast.Call)
    ]
    assert {name: calls.count(name) for name in COMPONENT_CONSTRUCTORS} == {
        name: 1 for name in COMPONENT_CONSTRUCTORS
    }


def test_runner_has_no_extracted_raw_execution_or_commit_helpers() -> None:
    runner = _runner_class()
    method_names = {
        node.name for node in runner.body if isinstance(node, ast.FunctionDef)
    }
    assert {
        "_publish",
        "_store",
        "_acquire_confirmation_capability",
        "_execute_confirmed_commit",
        "_record_confirmation_execution_failure",
    }.isdisjoint(method_names)

    call_paths = {
        _attribute_path(node.func)
        for node in ast.walk(runner)
        if isinstance(node, ast.Call)
    }
    assert ("self", "tool_executor", "execute") not in call_paths
    assert ("self", "publisher", "publish") not in call_paths
    assert ("self", "external_executor", "execute") not in call_paths
    assert not any(path[:2] == ("self", "result_store") for path in call_paths)


def test_extracted_lower_layers_do_not_import_runner_or_concrete_agents() -> None:
    forbidden_agent_modules = (
        ".agents.sleepcare",
        ".agents.evidence_reasoning",
        ".agents.care_strategy",
        ".agents.safety_review",
    )
    for path in EXTRACTED_COMPONENT_PATHS:
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.endswith(".runner"), path.name
                assert not module.endswith(".episode"), path.name
                assert not module.endswith(forbidden_agent_modules), path.name
                assert CONCRETE_AGENT_NAMES.isdisjoint(
                    alias.name for alias in node.names
                ), path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.endswith(".runner"), path.name
                    assert not alias.name.endswith(".episode"), path.name
                    assert not alias.name.endswith(forbidden_agent_modules), path.name


@pytest.mark.parametrize(
    ("method_name", "argument_name"),
    (
        ("run", "request"),
        ("reexecute_with_added_fact", "command"),
        ("commit_frozen_confirmations", "command"),
    ),
)
def test_three_runner_facades_keep_exact_serialized_signatures(
    method_name: str,
    argument_name: str,
) -> None:
    runner = _runner_class()
    method = next(
        node
        for node in runner.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    assert [_expression_name(item) for item in method.decorator_list] == [
        "serialized_episode_execution"
    ]

    runtime_method = getattr(ProductEpisodeRunner, method_name)
    assert hasattr(runtime_method, "__wrapped__")
    signature = inspect.signature(runtime_method)
    assert tuple(signature.parameters) == ("self", argument_name)
    assert all(
        parameter.default is inspect.Parameter.empty
        and parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _waiting_user_checkpoint() -> tuple[
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductUserFactResponse,
]:
    now = datetime.now(timezone.utc)
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=now,
        timezone_name="UTC",
        date_start=now.date(),
        date_end=now.date(),
        valid_night_count=1,
    )
    snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot:runner-contraction",
        binding=AuthenticatedBinding(
            actor_id="elder:runner-contraction",
            subject_id="subject:runner-contraction",
            role="elder",
            authorization_scope=("read_sleep_data",),
        ),
        source_scope=scope,
        canonical_data_version="canonical:v1",
        source_refs=("night:runner-contraction",),
        created_at=now,
    )
    request = ProductEpisodeRunRequest(
        episode_id="episode:runner-contraction",
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="answer with one authenticated added fact",
        fact_snapshot=snapshot,
        user_text="How was last night?",
    )
    pending = PendingUserInputTarget(
        request_id="fact-request:bedtime",
        question_text="Did you go to bed later than usual?",
        why_needed="Distinguish a routine change from a sensing change.",
        decision_scope="morning_review",
        target_role="elder",
        source_agent=AgentId.EVIDENCE_REASONING,
        expires_at=now + timedelta(minutes=20),
    )
    receipt = EpisodeReceipt(
        episode_id=request.episode_id,
        episode_type=request.episode_type,
        receipt_revision=1,
        terminal=False,
        execution_mode=ExecutionMode.INTELLIGENT,
        status=EpisodeStatus.WAITING_USER,
        goal_achieved=False,
        fact_snapshot_id=snapshot.fact_snapshot_id,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        source_scope=scope,
        final_episode_state_revision=1,
        trace_ref="trace:runner-contraction:1",
    )
    frozen = ProductEpisodeRunResult(
        registry_hash=stable_hash("runner-contraction-registry"),
        continuation_request_hash=product_episode_request_hash(request),
        receipt=receipt,
        pending_user_input=pending,
    )
    added_fact = ProductUserFactResponse(
        request_id=pending.request_id,
        answer="Yes, about forty minutes later.",
        actor_id=snapshot.binding.actor_id,
        actor_role="elder",
        subject_id=snapshot.binding.subject_id,
        observed_at=now,
    )
    return request, bind_product_episode_checkpoint(frozen), added_fact


class _ReexecutionWorkProbe:
    def __init__(self) -> None:
        self.commands: list[ReexecuteWithAddedFact] = []

    def reexecute_with_added_fact(
        self,
        command: ReexecuteWithAddedFact,
    ) -> ProductEpisodeRunRequest:
        self.commands.append(command)
        return command.reexecution_request()


def _dispatch_reexecution(
    probe: _ReexecutionWorkProbe,
    *,
    request: ProductEpisodeRunRequest,
    frozen: ProductEpisodeRunResult,
    added_fact: ProductUserFactResponse,
) -> ProductEpisodeRunRequest:
    command = ReexecuteWithAddedFact(
        request=request,
        frozen_result=frozen,
        added_fact=added_fact,
    )
    return probe.reexecute_with_added_fact(command)


def test_added_fact_reexecution_rejects_request_hash_drift_before_runner_work() -> None:
    request, frozen, added_fact = _waiting_user_checkpoint()
    changed_request = request.model_copy(
        update={"objective": "a different checkpoint request"}
    )
    probe = _ReexecutionWorkProbe()

    with pytest.raises(
        ValidationError,
        match="added-fact base request does not match checkpoint",
    ):
        _dispatch_reexecution(
            probe,
            request=changed_request,
            frozen=frozen,
            added_fact=added_fact,
        )

    assert probe.commands == []


def test_added_fact_reexecution_rejects_expiry_before_runner_work() -> None:
    request, frozen, added_fact = _waiting_user_checkpoint()
    assert frozen.pending_user_input is not None
    expired = frozen.model_copy(
        update={
            "pending_user_input": frozen.pending_user_input.model_copy(
                update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
            )
        }
    )
    expired = bind_product_episode_checkpoint(expired)
    probe = _ReexecutionWorkProbe()

    with pytest.raises(ValidationError, match="added-fact request expired"):
        _dispatch_reexecution(
            probe,
            request=request,
            frozen=expired,
            added_fact=added_fact,
        )

    assert probe.commands == []


@pytest.mark.parametrize(
    "binding_update",
    (
        {"actor_id": "different-actor"},
        {"actor_role": "family"},
        {"subject_id": "different-subject"},
    ),
)
def test_added_fact_reexecution_rejects_binding_drift_before_runner_work(
    binding_update: dict[str, str],
) -> None:
    request, frozen, added_fact = _waiting_user_checkpoint()
    mismatched_fact = added_fact.model_copy(update=binding_update)
    probe = _ReexecutionWorkProbe()

    with pytest.raises(
        ValidationError,
        match="added fact does not match authenticated binding",
    ):
        _dispatch_reexecution(
            probe,
            request=request,
            frozen=frozen,
            added_fact=mismatched_fact,
        )

    assert probe.commands == []


def test_valid_added_fact_reexecution_changes_only_the_response_set() -> None:
    request, frozen, added_fact = _waiting_user_checkpoint()
    probe = _ReexecutionWorkProbe()

    resumed = _dispatch_reexecution(
        probe,
        request=request,
        frozen=frozen,
        added_fact=added_fact,
    )

    assert probe.commands and probe.commands[0].request is request
    assert resumed.fact_snapshot == request.fact_snapshot
    assert resumed.episode_id == request.episode_id
    assert resumed.user_fact_responses == (added_fact,)
    assert resumed.continuation_lineage is not None
    assert resumed.continuation_lineage.kind == "reexecute_with_added_fact"
    assert product_episode_request_hash(resumed) != product_episode_request_hash(
        request
    )


class _ApiReexecutionRoutingProbe(UserInputProductRunner):
    def __init__(self) -> None:
        super().__init__()
        self._inside_reexecution = False
        self.direct_resume_run_calls = 0

    def run(self, request: ProductEpisodeRunRequest) -> ProductEpisodeRunResult:
        if self.requests and not self._inside_reexecution:
            self.direct_resume_run_calls += 1
        return super().run(request)

    def reexecute_with_added_fact(
        self,
        command: ReexecuteWithAddedFact,
    ) -> ProductEpisodeRunResult:
        self._inside_reexecution = True
        try:
            return super().reexecute_with_added_fact(command)
        finally:
            self._inside_reexecution = False


def test_product_api_routes_user_fact_through_reexecution_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = _ApiReexecutionRoutingProbe()
    reset_radar_api_runtime_for_tests(
        product_runtime=_product_runtime_with_runner(runner)
    )
    headers = {
        "x-api-key": API_KEY,
        "x-actor-id": API_ACTOR_ID,
        "x-actor-role": "family",
    }
    try:
        with TestClient(app) as client:
            created = client.post(
                "/radar-agent/tasks",
                headers={**headers, "Idempotency-Key": "runner-contraction-input"},
                json={"goal_type": "night_review"},
            )
            task_id = created.json()["task"]["task_id"]
            first = client.post(
                f"/radar-agent/tasks/{task_id}/run",
                headers=headers,
            )
            pending = first.json()["user_input_requests"][0]
            answered = client.post(
                f"/radar-agent/tasks/{task_id}/user-input",
                headers=headers,
                json={
                    "request_id": pending["request_id"],
                    "answer": "Yes, later than usual.",
                },
            )
    finally:
        reset_radar_api_runtime_for_tests()

    assert created.status_code == 200
    assert first.status_code == 200
    assert answered.status_code == 202
    assert runner.direct_resume_run_calls == 0
    assert len(runner.reexecute_commands) == 1
    assert runner.reexecute_commands[0].added_fact.answer == "Yes, later than usual."
