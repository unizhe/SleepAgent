from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
import server as legacy_server
from backend.main import app
from sleepagent.integrations.perceptor.push_ingestion import (
    PerceptorPushConfigurationError,
)
from sleepagent.integrations.perceptor.push_runtime import (
    build_perceptor_push_runtime_from_env,
)
from sleepagent.integrations.perceptor.webhook import (
    PerceptorWebhookConfigurationError,
    build_perceptor_webhook_service_from_env,
)
from sleepagent.radar_agent.product_agent import runtime_factory


def test_old_radar_surfaces_are_not_exposed_outside_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLEEPAGENT_RADAR_AGENT_DEV_MODE", "false")
    monkeypatch.setenv("SLEEPAGENT_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_RADAR_API_KEY", "configured-key")
    with TestClient(app) as client:
        assert client.get(
            "/product/radar/devices",
            headers={"x-api-key": "configured-key"},
        ).status_code == 404
        assert client.post(
            "/radar-agent/tasks",
            headers={"x-api-key": "configured-key"},
            json={},
        ).status_code == 404


def test_fake_provider_requires_explicit_dev_and_fake_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLEEPAGENT_RADAR_AGENT_DEV_MODE", "true")
    monkeypatch.setenv("SLEEPAGENT_DEPLOYMENT_MODE", "test")
    monkeypatch.delenv("SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE", raising=False)
    monkeypatch.setattr(backend_main, "_RADAR_PRODUCT_PROVIDER", None)
    with pytest.raises(RuntimeError, match="explicit development/test"):
        backend_main._radar_product_data_provider()


def test_legacy_probe_is_explicit_and_never_writes_authority_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPAGENT_LEGACY_PERCEPTOR_DIAGNOSTIC", "true")
    monkeypatch.setenv("SLEEPAGENT_DEPLOYMENT_MODE", "test")
    summary = legacy_server.summarize_diagnostic_payload(
        {"type": "VitalSignsDataEvent", "data": {"HeartRate": 61}}
    )

    assert legacy_server.diagnostic_enabled() is True
    assert legacy_server.DIAGNOSTIC_PATH == "/diagnostics/perceptor/receive"
    assert summary == {
        "event_type": "VitalSignsDataEvent",
        "message_id_present": False,
        "device_identifier_present": False,
        "data_type": "dict",
    }
    assert not (tmp_path / "radar_data.csv").exists()
    assert not tuple(tmp_path.glob("*.sqlite3"))


def test_product_runtime_production_cannot_fall_back_to_tmp_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLEEPAGENT_DEPLOYMENT_MODE", "production")
    monkeypatch.delenv("SLEEPAGENT_RADAR_AGENT_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="requires"):
        runtime_factory._persistence_store_from_env()


def test_production_push_rejects_replay_namespace_before_storage(
) -> None:
    values = {
        "SLEEPAGENT_PERCEPTOR_PUSH_ENABLED": "true",
        "SLEEPAGENT_PERCEPTOR_PUSH_NAMESPACE": "replay:not-production",
        "SLEEPAGENT_PERCEPTOR_PUSH_PROVIDER_ACCOUNT_ID": "account",
        "SLEEPAGENT_PERCEPTOR_PUSH_PROFILE_ID": "profile",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNING_SECRET": "secret",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_ALGORITHM": "HMAC-SHA256",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_MODE": "raw_body",
        "SLEEPAGENT_PERCEPTOR_PUSH_ENVIRONMENT": "production",
        "SLEEPAGENT_PERCEPTOR_PUSH_CONFIGURATION_FINGERPRINT": "a" * 64,
        "SLEEPAGENT_PERCEPTOR_PUSH_ADAPTER_ARTIFACT_SHA256": "b" * 64,
    }
    with pytest.raises(
        PerceptorPushConfigurationError,
        match="live namespace",
    ):
        build_perceptor_push_runtime_from_env(environ=values)


def test_legacy_webhook_sqlite_builder_is_diagnostic_only() -> None:
    with pytest.raises(
        PerceptorWebhookConfigurationError,
        match="diagnostic",
    ):
        build_perceptor_webhook_service_from_env(
            {
                "SLEEPAGENT_DEPLOYMENT_MODE": "production",
                "SLEEPAGENT_LEGACY_PERCEPTOR_DIAGNOSTIC": "true",
            }
        )
