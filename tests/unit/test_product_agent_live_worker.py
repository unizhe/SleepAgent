from __future__ import annotations

import json

import pytest

from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.integrations.llm import CloudLLMConfig
from sleepagent.product_device.llm import (
    DEFAULT_PRODUCT_LLM_BASE_URL,
    DEFAULT_PRODUCT_LLM_MODEL,
    OpenAICompatibleChatProvider,
    PRODUCT_LLM_API_KEY_ENV,
    ProductLLMConfigurationError,
    openai_compatible_provider_config_from_env,
)
from sleepagent.product_runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.product_runtime.postgres_worker import (
    ProductAgentCompositionError,
    ProductAgentProcessor,
    ProductAgentWorkHandlerAdapter,
    build_product_agent_worker_handlers,
)
from sleepagent.product_runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
)
from sleepagent.worker_runtime import _cli_handlers


pytestmark = pytest.mark.unit
SECRET = "sk-live-worker-secret-that-must-not-leak"


def _settings(
    model_mode: ModelMode,
    *,
    queues: tuple[str, ...] = ("product_agent",),
) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="product-live-worker-test",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:",),
        worker_queues=queues,
        provider_mode=(
            ProviderMode.LIVE
            if model_mode is ModelMode.LIVE
            else ProviderMode.FAKE
        ),
        model_mode=model_mode,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )


def _processor(handler: ProductAgentWorkHandlerAdapter) -> ProductAgentProcessor:
    factory = handler._processor_factory
    assert factory is not None
    return factory(object())  # type: ignore[arg-type]


def _models(handler: ProductAgentWorkHandlerAdapter) -> tuple[object, ...]:
    roster = _processor(handler).runtime_bundle.roster
    return (
        roster.sleepcare.planning_model,
        *(agent.model for agent in roster),
    )


def test_deterministic_worker_selection_remains_the_exact_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PRODUCT_LLM_API_KEY_ENV, raising=False)

    handler = build_product_agent_worker_handlers(
        _settings(ModelMode.DETERMINISTIC)
    )["product_agent"]

    assert isinstance(handler, ProductAgentWorkHandlerAdapter)
    models = _models(handler)
    assert len(models) == 5
    assert len({id(model) for model in models}) == 1
    assert all(
        isinstance(model, DeterministicReplayStructuredAgentModel)
        for model in models
    )
    assert handler._model_mode is ModelMode.DETERMINISTIC


def test_live_worker_selection_uses_existing_structured_agent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PRODUCT_LLM_API_KEY_ENV, SECRET)
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_MODEL", "local-model")
    monkeypatch.setenv(
        "SLEEPAGENT_PRODUCT_LLM_BASE_URL",
        "http://127.0.0.1:8765/v1",
    )
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS", "321")

    handler = build_product_agent_worker_handlers(_settings(ModelMode.LIVE))[
        "product_agent"
    ]

    assert isinstance(handler, ProductAgentWorkHandlerAdapter)
    models = _models(handler)
    assert len(models) == 5
    assert all(
        isinstance(model, OpenAICompatibleStructuredAgentModel)
        for model in models
    )
    assert all(model.model_id == "local-model" for model in models)
    assert all(model.config.base_url == "http://127.0.0.1:8765/v1" for model in models)
    assert all(model.config.timeout_seconds == 2.5 for model in models)
    assert all(model.config.max_output_tokens == 321 for model in models)
    assert handler._model_mode is ModelMode.LIVE
    assert _processor(handler).runtime_bundle.persistence_store is None


def test_standard_replay_worker_registry_composes_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PRODUCT_LLM_API_KEY_ENV, SECRET)
    queues = (
        "ingestion",
        "fast_path",
        "product_agent",
        "sleep_command",
        "product_interaction",
        "demo_advance",
        "replay_journey",
        "reconciliation",
    )

    handlers = _cli_handlers(
        _settings(ModelMode.LIVE, queues=queues)
    )

    assert set(handlers) == set(queues)


@pytest.mark.parametrize(
    "configured_key",
    [None, "", "   ", "<deepseek-api-key>"],
)
def test_live_worker_missing_key_fails_composition_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    configured_key: str | None,
) -> None:
    if configured_key is None:
        monkeypatch.delenv(PRODUCT_LLM_API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(PRODUCT_LLM_API_KEY_ENV, configured_key)

    with pytest.raises(ProductAgentCompositionError, match=PRODUCT_LLM_API_KEY_ENV):
        build_product_agent_worker_handlers(_settings(ModelMode.LIVE))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SLEEPAGENT_PRODUCT_LLM_MODEL", "<model>"),
        ("SLEEPAGENT_PRODUCT_LLM_BASE_URL", "https://secret@provider.example/v1"),
        ("SLEEPAGENT_PRODUCT_LLM_BASE_URL", "http://provider.example/v1"),
        ("SLEEPAGENT_PRODUCT_LLM_BASE_URL", "https://\nprovider.example/v1"),
        ("SLEEPAGENT_PRODUCT_LLM_BASE_URL", "https://provider example/v1"),
        ("SLEEPAGENT_PRODUCT_LLM_BASE_URL", "https://%zz/v1"),
        ("SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS", "nan"),
        ("SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS", "0"),
        ("SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS", "1.5"),
        ("SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS", "0"),
    ],
)
def test_live_worker_invalid_configuration_fails_closed_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(PRODUCT_LLM_API_KEY_ENV, SECRET)
    monkeypatch.setenv(name, value)

    with pytest.raises(ProductAgentCompositionError) as exc:
        build_product_agent_worker_handlers(_settings(ModelMode.LIVE))

    assert name in str(exc.value)
    assert SECRET not in str(exc.value)
    assert value not in str(exc.value)


def test_documented_product_llm_defaults_are_the_runtime_defaults() -> None:
    config = openai_compatible_provider_config_from_env({})

    assert config.model == DEFAULT_PRODUCT_LLM_MODEL
    assert config.base_url == DEFAULT_PRODUCT_LLM_BASE_URL
    assert config.timeout_seconds == 30
    assert config.max_output_tokens == 1200


def test_secret_is_excluded_from_provider_and_cloud_config_public_forms() -> None:
    provider = OpenAICompatibleChatProvider(api_key=SECRET)
    cloud = CloudLLMConfig(
        base_url="https://provider.example/v1",
        api_key=SECRET,
        model_id="provider-model",
    )

    public_forms = (
        repr(provider),
        repr(cloud),
        json.dumps(cloud.model_dump(mode="json"), sort_keys=True),
    )
    assert all(SECRET not in value for value in public_forms)
    assert "api_key" not in cloud.model_dump(mode="json")
    assert not hasattr(provider, "api_key")


def test_configuration_error_type_never_includes_secret() -> None:
    with pytest.raises(ProductLLMConfigurationError) as exc:
        openai_compatible_provider_config_from_env(
            {
                PRODUCT_LLM_API_KEY_ENV: SECRET,
                "SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS": SECRET,
            }
        )

    current: BaseException | None = exc.value
    while current is not None:
        assert SECRET not in str(current)
        assert SECRET not in repr(current)
        current = current.__cause__
