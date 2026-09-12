from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace

import pytest

from sleepagent.runtime.agents import (
    AGENT_IMPLEMENTATIONS,
    CareStrategyAgent,
    EvidenceReasoningAgent,
    ProductAgentFactory,
    ProductAgentRoster,
    SafetyReviewAgent,
    SleepCareAgent,
    concrete_agent_manifest,
    validate_concrete_agent_manifest,
)
from sleepagent.runtime.contracts import (
    AgentId,
    PRODUCT_AGENT_CONTRACT_VERSION,
    PRODUCT_AGENT_ROSTER,
    stable_hash,
)
from sleepagent.config import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.runtime.agent_invocation_coordinator import (
    AgentInvocationCoordinator,
)
from sleepagent.runtime.registry import product_agent_manifest
from sleepagent.runtime.runner import ProductEpisodeRunner
from sleepagent.runtime.registry import (
    SkillRegistry,
    default_skill_packages,
)
import sleepagent.runtime.factory as runtime_factory
from sleepagent.runtime.factory import (
    build_product_episode_runner_from_env,
    build_product_runtime_bundle,
    product_episode_runner_is_configured,
)
from sleepagent.workers.product import build_product_agent_worker_handlers


class NeverCalledModel:
    provider = "test"
    model_id = "never-called"

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        raise AssertionError("factory test must not call a model")


def build_roster() -> ProductAgentRoster:
    model = NeverCalledModel()
    registry = SkillRegistry(default_skill_packages())
    return ProductAgentFactory.create(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        skill_registry=registry,
    )


def test_factory_constructs_exact_immutable_four_role_roster() -> None:
    roster = build_roster()
    assert tuple(roster.as_mapping()) == PRODUCT_AGENT_ROSTER
    assert tuple(type(item) for item in roster) == (
        SleepCareAgent,
        EvidenceReasoningAgent,
        CareStrategyAgent,
        SafetyReviewAgent,
    )
    assert type(roster.safety_review) is SafetyReviewAgent
    assert all(not hasattr(item, "invoker") for item in roster)
    with pytest.raises(TypeError):
        roster.as_mapping()[AgentId.SLEEP_CARE] = roster.sleepcare  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        roster.sleepcare = roster.sleepcare  # type: ignore[misc]


def test_evidence_skills_compile_semantic_contract_instructions() -> None:
    package = SkillRegistry(default_skill_packages()).champion(
        "interpret_scoped_evidence",
        AgentId.EVIDENCE_REASONING,
    )

    instructions = " ".join(package.instructions)
    assert package.version == "3.0.0"
    assert "alternative_explanation" in instructions
    assert "If Context contains none, omit" in instructions
    assert "never invent a decision" in instructions
    assert "claim_strength" in instructions
    assert "readiness_decision_ref" in instructions
    assert "source_kind confirmed_habit" in instructions
    assert "exact fact_ref" in instructions
    assert "source_kind confirmed_memory" in instructions
    assert "exact retrieval_handle" in instructions
    assert "vendor-derived" in instructions
    assert "reconstructed cadence timestamps" in instructions
    assert "deterministic quality is partial" in instructions


def test_communication_and_safety_skills_compile_contract_instructions() -> None:
    registry = SkillRegistry(default_skill_packages())
    communication = registry.champion(
        "explain_for_elder",
        AgentId.SLEEP_CARE,
    )
    safety = registry.champion(
        "review_claim_and_boundary",
        AgentId.SAFETY_REVIEW,
    )

    communication_instructions = " ".join(communication.instructions)
    assert communication.version == "4.0.0"
    assert communication.output_schema_id == "CommunicationDraft"
    assert "Return only a SleepCareContentPlan" in communication_instructions
    assert "source_type and source_ref" in communication_instructions
    assert "do not generate the final Communication text" in (
        communication_instructions
    )
    assert "rendered_text" in communication_instructions
    assert "Never guess" in communication_instructions
    assert "empty selected_segments" in communication_instructions
    assert "approve is never an envelope status" in " ".join(safety.instructions)
    assert "9999-12-31T23:59:59Z" in " ".join(safety.instructions)


@pytest.mark.parametrize(
    "skill_id",
    [
        "answer_grounded_question",
        "explain_for_elder",
        "draft_user_material",
        "draft_doctor_material",
    ],
)
def test_every_sleepcare_communication_skill_uses_versioned_content_plan_contract(
    skill_id: str,
) -> None:
    package = SkillRegistry(default_skill_packages()).champion(
        skill_id,
        AgentId.SLEEP_CARE,
    )

    assert package.version == "4.0.0"
    assert package.output_schema_id == "CommunicationDraft"
    assert "SleepCareContentPlan" in " ".join(package.instructions)


def test_factory_rejects_missing_extra_and_plain_string_role_bindings() -> None:
    model = NeverCalledModel()
    missing = {
        AgentId.SLEEP_CARE: model,
        AgentId.EVIDENCE_REASONING: model,
        AgentId.CARE_STRATEGY: model,
    }
    with pytest.raises(ValueError, match="exact typed Agent roster"):
        ProductAgentFactory.from_models(missing)

    string_aliases = {
        "sleep_care": model,
        "evidence_reasoning": model,
        "care_strategy": model,
        "safety_review": model,
    }
    with pytest.raises(ValueError, match="exact typed Agent roster"):
        ProductAgentFactory.from_models(string_aliases)  # type: ignore[arg-type]


def test_concrete_manifest_matches_contract_registry_without_changing_identity() -> None:
    validate_concrete_agent_manifest()
    manifest = concrete_agent_manifest()
    assert tuple(AGENT_IMPLEMENTATIONS) == PRODUCT_AGENT_ROSTER
    assert tuple(manifest["agents"]) == tuple(
        item.value for item in PRODUCT_AGENT_ROSTER
    )
    assert stable_hash(product_agent_manifest()) == (
        "8a618d48b1e5938d1e5fdca41172a0e7a9ba2c4e63b80bb1a4c73baa50e4505e"
    )
    assert PRODUCT_AGENT_CONTRACT_VERSION == "sleepagent-product-agent.v15"


def test_live_product_worker_accepts_live_canonical_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-structural-test-only")
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_MODEL", "structural-model")
    monkeypatch.setenv(
        "SLEEPAGENT_PRODUCT_LLM_BASE_URL",
        "https://provider.invalid/v1",
    )
    settings = SleepBackendSettings(
        profile="p4e2-live-agent-structure",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.LIVE,
        database_dsn="postgresql://worker@127.0.0.1/p4e2",
        database_identity="p4e2",
        database_role="worker",
        service_principal_id="p4e2-worker",
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:p4e2",),
        worker_queues=("product_agent",),
        model_mode=ModelMode.LIVE,
        service_credential_ref="env:P4E2_SERVICE_CREDENTIAL",
        signing_key_ref="env:P4E2_SIGNING_KEY",
        encryption_key_ref="env:P4E2_ENCRYPTION_KEY",
    )

    handlers = build_product_agent_worker_handlers(settings)

    assert tuple(handlers) == ("product_agent",)


def test_concrete_roles_declare_distinct_context_and_permission_boundaries() -> None:
    roster = build_roster()
    sleepcare = roster.sleepcare.boundary
    evidence = roster.evidence_reasoning.boundary
    care = roster.care_strategy.boundary
    safety = roster.safety_review.boundary

    assert sleepcare.context.raw_user_text_visible
    assert evidence.context.raw_user_text_visible
    assert not care.context.raw_user_text_visible
    assert not safety.context.raw_user_text_visible
    assert care.context.required_context_keys == ("accepted:evidence_packet",)
    assert safety.context.required_context_keys == ("safety_review_target",)
    assert tuple(item.operation for item in sleepcare.control_ports) == (
        "plan",
        "evaluate",
    )
    assert not evidence.control_ports
    assert not care.control_ports
    assert not safety.control_ports
    assert care.context.visible_accepted_agents == (
        AgentId.EVIDENCE_REASONING,
    )
    assert safety.context.visible_accepted_agents == ()
    assert sleepcare.may_publish
    assert all(
        not boundary.may_mutate_shared_state
        and not boundary.may_execute_side_effects
        and boundary.success_conditions
        and boundary.failure_conditions
        for boundary in (sleepcare, evidence, care, safety)
    )


def test_runner_delegates_provider_calls_through_invocation_coordinator() -> None:
    runner = build_product_runtime_bundle(agent_roster=build_roster()).runner
    assert tuple(runner.agent_roster.as_mapping()) == PRODUCT_AGENT_ROSTER
    runner_source = inspect.getsource(ProductEpisodeRunner._invoke_and_accept)
    coordinator_source = inspect.getsource(AgentInvocationCoordinator.invoke_turn)
    assert "agent_invocation_coordinator.invoke_turn(" in runner_source
    assert "agent.bind(" in coordinator_source
    assert "agent.invoke_bound(" in coordinator_source
    assert "agent_id: AgentId" not in runner_source
    assert "self.invokers[" not in runner_source
    assert not hasattr(runner, "invokers")
    assert not hasattr(runner, "sleepcare_model")


def test_runner_configuration_probe_requires_exact_concrete_roster() -> None:
    assert not product_episode_runner_is_configured(object())  # type: ignore[arg-type]
    runner = build_product_runtime_bundle(agent_roster=build_roster()).runner
    assert product_episode_runner_is_configured(runner)
    runner.agent_roster = None  # type: ignore[assignment]
    assert not product_episode_runner_is_configured(runner)

    model = NeverCalledModel()
    duck_runner = SimpleNamespace(
        sleepcare_model=model,
        invokers={
            agent_id: SimpleNamespace(model=model)
            for agent_id in PRODUCT_AGENT_ROSTER
        },
    )
    assert not product_episode_runner_is_configured(duck_runner)  # type: ignore[arg-type]


def test_runner_requires_the_complete_runtime_graph() -> None:
    with pytest.raises(TypeError):
        ProductEpisodeRunner(agent_roster=build_roster())  # type: ignore[call-arg]

    signature = inspect.signature(ProductEpisodeRunner)
    legacy_arguments = {
        "agent_models",
        "invokers",
        "model",
        "sleepcare_model",
        "source_resolvers",
    }
    assert legacy_arguments.isdisjoint(signature.parameters)
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
    assert all(
        parameter.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for parameter in signature.parameters.values()
    )


def test_runner_compatibility_factory_returns_the_canonical_bundle_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(
        runtime_factory,
        "build_product_runtime_bundle_from_env",
        lambda **_kwargs: SimpleNamespace(runner=sentinel),
    )

    assert build_product_episode_runner_from_env() is sentinel
