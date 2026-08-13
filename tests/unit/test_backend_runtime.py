from __future__ import annotations

import asyncio

import pytest

from sleepagent.process import (
    DatabaseAttestation,
    RuntimeServices,
    SleepBackendRuntime,
)
from sleepagent.config import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
)
from tests.support.runtime_fixtures import (
    reset_backend_runtime_state as reset_active_runtime_for_tests,
)


pytestmark = pytest.mark.unit


class Pool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")


def _settings(role: ProcessRole = ProcessRole.API) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="test-replay",
        deployment_mode=DeploymentMode.TEST,
        process_role=role,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://api:top-secret@postgres/replay_db",
        database_identity="replay_db",
        database_role=f"sleepagent_{role.value}_replay",
        service_principal_id=f"sleepagent-{role.value}-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:pytest",),
        enabled_surfaces=(
            frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
            if role == ProcessRole.API
            else frozenset()
        ),
        worker_queues=(
            ("fast_path", "product_agent")
            if role == ProcessRole.WORKER
            else ()
        ),
        provider_mode=(
            ProviderMode.FAKE
            if role == ProcessRole.WORKER
            else ProviderMode.DISABLED
        ),
        model_mode=(
            ModelMode.DETERMINISTIC
            if role == ProcessRole.WORKER
            else ModelMode.DISABLED
        ),
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
        internal_auth_token="do-not-print-this-internal-token",
    )


def _runtime(
    role: ProcessRole = ProcessRole.API,
    *,
    pool: Pool | None = None,
    attestation: DatabaseAttestation | None = None,
    expected_actor_key: str | None = None,
) -> SleepBackendRuntime:
    settings = _settings(role)
    handlers = (
        {"fast_path": object(), "product_agent": object()}
        if role == ProcessRole.WORKER
        else None
    )
    return SleepBackendRuntime(
        settings,
        pool=pool or Pool(),
        uow_factory=object(),
        attestor=lambda: attestation
        or DatabaseAttestation(
            database_identity=settings.database_identity,
            database_role=settings.database_role,
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
        services=(RuntimeServices(product=object()) if role == ProcessRole.API else None),
        worker_handlers=handlers,
        expected_actor_verification_key_sha256=expected_actor_key,
    )


def setup_function() -> None:
    reset_active_runtime_for_tests()


def teardown_function() -> None:
    reset_active_runtime_for_tests()


def test_api_runtime_rejects_worker_handlers() -> None:
    with pytest.raises(ValueError, match="API runtime"):
        SleepBackendRuntime(
            _settings(),
            pool=Pool(),
            uow_factory=object(),
            attestor=lambda: None,  # type: ignore[arg-type]
            worker_handlers={"product_agent": object()},
        )


def test_worker_runtime_requires_every_configured_queue_handler() -> None:
    with pytest.raises(ValueError, match="fast_path"):
        SleepBackendRuntime(
            _settings(ProcessRole.WORKER),
            pool=Pool(),
            uow_factory=object(),
            attestor=lambda: None,  # type: ignore[arg-type]
            worker_handlers={"product_agent": object()},
        )


def test_runtime_lifespan_opens_only_pool_and_attests_database() -> None:
    pool = Pool()
    runtime = _runtime(pool=pool)

    asyncio.run(runtime.start())

    assert runtime.started is True
    assert pool.calls == ["open"]
    assert runtime.readiness()["ready"] is True

    asyncio.run(runtime.close())
    assert pool.calls == ["open", "close"]


def test_only_one_runtime_can_be_active_per_process() -> None:
    first = _runtime()
    second = _runtime()
    asyncio.run(first.start())
    try:
        with pytest.raises(RuntimeError, match="already active"):
            asyncio.run(second.start())
    finally:
        asyncio.run(first.close())


def test_attestation_mismatch_fails_closed_and_closes_pool() -> None:
    pool = Pool()
    runtime = _runtime(
        pool=pool,
        attestation=DatabaseAttestation(
            database_identity="wrong_database",
            database_role="sleepagent_api_replay",
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
    )

    with pytest.raises(RuntimeError, match="identity"):
        asyncio.run(runtime.start())
    assert pool.calls == ["open", "close"]
    assert runtime.started is False


def test_replay_actor_key_registry_mismatch_fails_readiness_closed() -> None:
    pool = Pool()
    settings = _settings()
    expected = "a" * 64
    runtime = _runtime(
        pool=pool,
        expected_actor_key=expected,
        attestation=DatabaseAttestation(
            database_identity=settings.database_identity,
            database_role=settings.database_role,
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
            actor_verification_key_sha256s=("b" * 64,),
        ),
    )

    with pytest.raises(RuntimeError, match="actor verification key"):
        asyncio.run(runtime.start())
    assert pool.calls == ["open", "close"]


def test_dependency_manifest_is_capability_scoped_and_secret_free() -> None:
    api = _runtime()
    manifest = api.dependency_manifest().model_dump_json()

    assert '"process_role":"api"' in manifest
    assert '"enabled_handlers":[]' in manifest
    assert '"model_mode":"disabled"' in manifest
    assert "top-secret" not in manifest
    assert "do-not-print" not in manifest

    worker = _runtime(ProcessRole.WORKER)
    worker_manifest = worker.dependency_manifest()
    assert worker_manifest.enabled_handlers == ("fast_path", "product_agent")
    assert worker_manifest.enabled_surfaces == ()
