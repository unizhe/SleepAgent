from __future__ import annotations

import pytest
from pydantic import ValidationError

from sleepagent.backend_settings import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)


pytestmark = pytest.mark.unit


def _settings(**overrides):
    values = {
        "profile": "test-replay",
        "deployment_mode": DeploymentMode.TEST,
        "process_role": ProcessRole.API,
        "data_mode": DataMode.REPLAY,
        "database_dsn": "postgresql://api:secret@postgres/replay_db",
        "database_identity": "replay-db-1",
        "database_role": "sleepagent_api_replay",
        "service_principal_id": "sleepagent-api-test",
        "database_scope": DataMode.REPLAY,
        "namespace_prefixes": ("replay:pytest",),
        "enabled_surfaces": frozenset(
            {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
        ),
        "service_credential_ref": "test:service-credential",
        "signing_key_ref": "test:signing",
        "encryption_key_ref": "test:encryption",
    }
    values.update(overrides)
    return SleepBackendSettings(**values)


def test_settings_are_frozen_and_fingerprint_redacts_secrets() -> None:
    settings = _settings(internal_auth_token="never-print-this-token")

    assert len(settings.public_fingerprint()) == 64
    assert "secret" not in repr(settings)
    assert "never-print-this-token" not in repr(settings)
    with pytest.raises(ValidationError):
        settings.profile = "changed"  # type: ignore[misc]


def test_api_profile_cannot_reach_worker_or_model_capabilities() -> None:
    with pytest.raises(ValidationError, match="API process cannot own"):
        _settings(worker_queues=("fast_path",))
    with pytest.raises(ValidationError, match="cannot load a model"):
        _settings(model_mode=ModelMode.DETERMINISTIC)
    with pytest.raises(ValidationError, match="cannot load a provider"):
        _settings(provider_mode=ProviderMode.FAKE)


def test_worker_requires_a_queue_and_cannot_expose_api_surface() -> None:
    with pytest.raises(ValidationError, match="at least one queue"):
        _settings(
            process_role=ProcessRole.WORKER,
            enabled_surfaces=frozenset(),
        )

    worker = _settings(
        process_role=ProcessRole.WORKER,
        enabled_surfaces=frozenset(),
        worker_queues=("fast_path", "product_agent"),
        model_mode=ModelMode.DETERMINISTIC,
        provider_mode=ProviderMode.FAKE,
    )
    assert worker.worker_queues == ("fast_path", "product_agent")


def test_database_scope_and_namespace_prefix_are_not_row_only_isolation() -> None:
    with pytest.raises(ValidationError, match="database_scope"):
        _settings(database_scope=DataMode.LIVE)
    with pytest.raises(ValidationError, match="namespace prefixes"):
        _settings(namespace_prefixes=("live:wrong",))


def test_production_fails_closed_for_replay_demo_fake_and_default_keys() -> None:
    with pytest.raises(ValidationError, match="production requires live"):
        _settings(deployment_mode=DeploymentMode.PRODUCTION)

    with pytest.raises(ValidationError, match="demo surface"):
        _settings(
            deployment_mode=DeploymentMode.PRODUCTION,
            data_mode=DataMode.LIVE,
            database_scope=DataMode.LIVE,
            namespace_prefixes=("live:default",),
            enabled_surfaces=frozenset({ApiSurface.DEMO}),
            signing_key_ref="kms:signing",
            encryption_key_ref="kms:encryption",
            service_credential_ref="env:SLEEPAGENT_SERVICE_CREDENTIAL",
        )


def test_production_api_accepts_only_live_capability_scoped_configuration() -> None:
    settings = _settings(
        deployment_mode=DeploymentMode.PRODUCTION,
        data_mode=DataMode.LIVE,
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:default",),
        enabled_surfaces=frozenset(
            {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
        ),
        database_dsn="postgresql://api@postgres/sleepagent_live",
        database_identity="sleepagent-live-primary",
        database_role="sleepagent_api_live",
        signing_key_ref="kms://sleepagent/signing/current",
        encryption_key_ref="kms://sleepagent/encryption/current",
        service_credential_ref="env:SLEEPAGENT_SERVICE_CREDENTIAL",
    )

    assert settings.process_role == ProcessRole.API
    assert settings.model_mode == ModelMode.DISABLED
    assert settings.provider_mode == ProviderMode.DISABLED


def test_environment_parser_has_no_implicit_authority_defaults() -> None:
    with pytest.raises(ValueError, match="PROFILE is required"):
        SleepBackendSettings.from_environment({})

    settings = SleepBackendSettings.from_environment(
        {
            "SLEEPAGENT_BACKEND_PROFILE": "test-replay",
            "SLEEPAGENT_BACKEND_DEPLOYMENT_MODE": "test",
            "SLEEPAGENT_BACKEND_PROCESS_ROLE": "api",
            "SLEEPAGENT_BACKEND_DATA_MODE": "replay",
            "SLEEPAGENT_BACKEND_DATABASE_DSN": (
                "postgresql://api:secret@postgres/replay_db"
            ),
            "SLEEPAGENT_BACKEND_DATABASE_IDENTITY": "replay-db-1",
            "SLEEPAGENT_BACKEND_DATABASE_ROLE": "sleepagent_api_replay",
            "SLEEPAGENT_BACKEND_SERVICE_PRINCIPAL_ID": "sleepagent-api-test",
            "SLEEPAGENT_BACKEND_DATABASE_SCOPE": "replay",
            "SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES": "replay:one,replay:two",
            "SLEEPAGENT_BACKEND_SIGNING_KEY_REF": "test:signing",
            "SLEEPAGENT_BACKEND_ENCRYPTION_KEY_REF": "test:encryption",
            "SLEEPAGENT_BACKEND_SERVICE_CREDENTIAL_REF": (
                "test:service-credential"
            ),
            "SLEEPAGENT_BACKEND_INTERNAL_AUTH_TOKEN": (
                "test-internal-token-32-bytes-long"
            ),
        }
    )

    assert settings.enabled_surfaces == frozenset(
        {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT, ApiSurface.INTERNAL}
    )
    assert settings.namespace_prefixes == ("replay:one", "replay:two")
