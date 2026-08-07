from __future__ import annotations

import sqlite3

import pytest
from cryptography.fernet import Fernet

from sleepagent.integrations.perceptor.push_ingestion import (
    PerceptorPushConfigurationError,
)
from sleepagent.integrations.perceptor.push_runtime import (
    build_perceptor_push_runtime_from_env,
)
from sleepagent.radar_agent.persistence import RadarPersistenceStore


def _environment() -> dict[str, str]:
    return {
        "SLEEPAGENT_PERCEPTOR_PUSH_ENABLED": "true",
        "SLEEPAGENT_PERCEPTOR_PUSH_NAMESPACE": "replay:runtime-test",
        "SLEEPAGENT_PERCEPTOR_PUSH_PROVIDER_ACCOUNT_ID": "opaque-account",
        "SLEEPAGENT_PERCEPTOR_PUSH_PROFILE_ID": "profile.v1",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNING_SECRET": "signing-secret",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_ALGORITHM": "HMAC-SHA256",
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_MODE": "raw_body",
        "SLEEPAGENT_PERCEPTOR_PUSH_ENVIRONMENT": "test",
        "SLEEPAGENT_PERCEPTOR_PUSH_CONFIGURATION_FINGERPRINT": "a" * 64,
        "SLEEPAGENT_PERCEPTOR_PUSH_ADAPTER_ARTIFACT_SHA256": "b" * 64,
        "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY_ID": "runtime-test-key",
        "SLEEP_DOMAIN_RAW_RETENTION_SECONDS": "3600",
    }


def test_runtime_requires_explicit_fail_closed_configuration() -> None:
    with pytest.raises(PerceptorPushConfigurationError):
        build_perceptor_push_runtime_from_env(environ={})


def test_runtime_reuses_unified_store_and_restarts_idempotently() -> None:
    store = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    values = _environment()
    first = build_perceptor_push_runtime_from_env(
        environ=values,
        store=store,
    )
    first.close()
    assert store.connection.execute("SELECT 1").fetchone() == (1,)
    second = build_perceptor_push_runtime_from_env(
        environ=values,
        store=store,
    )
    assert second.ingestion.profiles["profile.v1"].capability_status == "pending"
    assert (
        second.repository.count_rows("sleep_domain_provider_accounts") == 1
    )
    second.close()
    store.connection.close()


def test_production_runtime_rejects_standalone_sqlite_fallback() -> None:
    values = _environment()
    values["SLEEPAGENT_PERCEPTOR_PUSH_NAMESPACE"] = "live:production"
    values["SLEEPAGENT_PERCEPTOR_PUSH_ENVIRONMENT"] = "production"
    with pytest.raises(
        PerceptorPushConfigurationError,
        match="standalone SQLite",
    ):
        build_perceptor_push_runtime_from_env(environ=values)
