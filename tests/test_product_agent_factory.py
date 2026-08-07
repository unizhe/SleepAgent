from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace

import pytest

from sleepagent.radar_agent.product_agent.agents import (
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
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    PRODUCT_AGENT_CONTRACT_VERSION,
    PRODUCT_AGENT_ROSTER,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.acceptance import (
    current_acceptance_release_identity,
)
from sleepagent.radar_agent.product_agent.registry import product_agent_manifest
from sleepagent.radar_agent.product_agent.runner import ProductEpisodeRunner
from sleepagent.radar_agent.product_agent.runtime_factory import (
    build_product_episode_runner_from_env,
    product_episode_runner_is_configured,
)


class NeverCalledModel:
    provider = "test"
    model_id = "never-called"

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        raise AssertionError("factory test must not call a model")


def build_roster() -> ProductAgentRoster:
    model = NeverCalledModel()
    return ProductAgentFactory.create(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
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
        "bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22"
    )
    assert current_acceptance_release_identity().identity_hash == (
        "02d4eff71d5d208288133237e6846da72ba45e984ac081e16544c90e47d344ae"
    )
    assert PRODUCT_AGENT_CONTRACT_VERSION == "sleepagent-product-agent.v14"


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


def test_runner_delegates_provider_calls_through_typed_agent_port() -> None:
    runner = ProductEpisodeRunner(agent_roster=build_roster())
    assert tuple(runner.agent_roster.as_mapping()) == PRODUCT_AGENT_ROSTER
    source = inspect.getsource(ProductEpisodeRunner._invoke_and_accept)
    assert "agent.bind(" in source
    assert "agent.invoke_bound(" in source
    assert "agent_id: AgentId" not in source
    assert "self.invokers[" not in source
    assert not hasattr(runner, "invokers")
    assert not hasattr(runner, "sleepcare_model")


def test_runner_configuration_probe_requires_exact_concrete_roster() -> None:
    assert not product_episode_runner_is_configured(object())  # type: ignore[arg-type]
    runner = ProductEpisodeRunner(agent_roster=build_roster())
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


def test_production_factory_explicitly_constructs_concrete_roles() -> None:
    source = inspect.getsource(build_product_episode_runner_from_env)
    assert "ProductAgentFactory.create(" in source
    assert "agent_models=" not in source
    assert "for agent_id in AgentId" not in source
