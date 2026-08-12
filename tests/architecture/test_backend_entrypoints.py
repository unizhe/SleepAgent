from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sleepagent.backend_app import create_sleep_backend_app
from sleepagent.backend_runtime import (
    DatabaseAttestation,
    SleepBackendRuntime,
)
from sleepagent.backend_services import build_api_runtime_services
from sleepagent.backend_settings import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
)


pytestmark = pytest.mark.unit
REPOSITORY = Path(__file__).parents[2]


def _subprocess_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SLEEPAGENT_BACKEND_")
        and key
        not in {
            "SLEEPAGENT_RADAR_AGENT_DEV_MODE",
            "SLEEPAGENT_DEPLOYMENT_MODE",
        }
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPOSITORY),
            **overrides,
        }
    )
    return environment


def _run_import(script: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY,
        env=_subprocess_environment(**environment),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_backend_main_has_no_legacy_import_or_compatibility_guard() -> None:
    tree = ast.parse((REPOSITORY / "backend" / "main.py").read_text())
    legacy_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "backend"
        and any(alias.name == "legacy_main" for alias in node.names)
    ]
    guarded = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and isinstance(node.test.func, ast.Name)
        and node.test.func.id == "_legacy_compatibility_requested"
    ]

    assert legacy_imports == []
    assert guarded == []


def test_configured_backend_main_import_does_not_load_legacy_module(
    tmp_path: Path,
) -> None:
    actor_public_key = tmp_path / "actor-public.pem"
    actor_public_key.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    result = _run_import(
        """
import json
import sys
import types

pool_module = types.ModuleType("psycopg_pool")

class ConnectionPool:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

pool_module.ConnectionPool = ConnectionPool
sys.modules["psycopg_pool"] = pool_module

import backend.main as entrypoint

print(json.dumps({
    "legacy_loaded": "tests.support.diagnostic_app" in sys.modules,
    "title": entrypoint.app.title,
}))
""",
        SLEEPAGENT_BACKEND_PROFILE="entrypoint-test",
        SLEEPAGENT_BACKEND_PROCESS_ROLE="api",
        SLEEPAGENT_BACKEND_DEPLOYMENT_MODE="test",
        SLEEPAGENT_BACKEND_DATA_MODE="replay",
        SLEEPAGENT_BACKEND_DATABASE_DSN=(
            "postgresql://entrypoint:secret@postgres/entrypoint_replay"
        ),
        SLEEPAGENT_BACKEND_DATABASE_IDENTITY="entrypoint_replay",
        SLEEPAGENT_BACKEND_DATABASE_ROLE="sleepagent_api_replay",
        SLEEPAGENT_BACKEND_SERVICE_PRINCIPAL_ID="entrypoint-test",
        SLEEPAGENT_BACKEND_DATABASE_SCOPE="replay",
        SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES="replay:entrypoint",
        SLEEPAGENT_BACKEND_ENABLED_SURFACES="public_v1,product",
        SLEEPAGENT_BACKEND_SIGNING_KEY_REF=f"file:{actor_public_key}",
        SLEEPAGENT_BACKEND_ENCRYPTION_KEY_REF="test:entrypoint-encryption",
        SLEEPAGENT_BACKEND_SERVICE_CREDENTIAL_REF=(
            "test:entrypoint-service-credential"
        ),
        SLEEPAGENT_BACKEND_POOL_MIN_SIZE="0",
        SLEEPAGENT_RADAR_AGENT_DEV_MODE="true",
        SLEEPAGENT_DEPLOYMENT_MODE="development",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "legacy_loaded": False,
        "title": "SleepAgent Backend",
    }


def test_backend_main_without_profile_or_explicit_compatibility_fails_closed() -> None:
    result = _run_import(
        """
import json
import sys

try:
    import backend.main
except Exception as exc:
    print(json.dumps({
        "error_type": type(exc).__name__,
        "message": str(exc),
        "legacy_loaded": "tests.support.diagnostic_app" in sys.modules,
    }))
else:
    raise SystemExit("backend.main unexpectedly started without a profile")
""",
        SLEEPAGENT_DEPLOYMENT_MODE="production",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["error_type"] == "ValueError"
    assert payload["message"] == "SLEEPAGENT_BACKEND_PROFILE is required"
    assert payload["legacy_loaded"] is False


def test_legacy_sleep_api_adapter_is_test_only_and_uses_canonical_factory() -> None:
    support = importlib.import_module("tests.support.runtime_fixtures")
    app = support.create_test_sleep_api_app(runtime_provider=lambda: object())

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.runtime.worker_handlers == {}

    asyncio.run(exercise())
    source = (
        REPOSITORY / "tests" / "support" / "runtime_fixtures.py"
    ).read_text()
    assert "create_sleep_backend_app(" in source
    assert "start_sleep_api_worker" not in source
    assert "stop_sleep_api_worker" not in source
    assert "app =" not in source
    assert not (REPOSITORY / "sleepagent" / "sleep_api" / "app.py").exists()

    runtime_source = (
        REPOSITORY / "sleepagent" / "sleep_api" / "runtime.py"
    ).read_text()
    assert "_RUNTIME" not in runtime_source
    assert "_WORKER" not in runtime_source
    assert "get_sleep_api_runtime" not in runtime_source
    assert "start_sleep_api_worker" not in runtime_source


def test_only_canonical_postgres_worker_directly_calls_product_runner() -> None:
    diagnostics = (
        REPOSITORY / "sleepagent" / "product_api" / "diagnostics" / "http.py"
    ).read_text(encoding="utf-8")
    bridge = (
        REPOSITORY / "sleepagent" / "sleep_domain" / "agent_bridge.py"
    ).read_text(encoding="utf-8")
    habit = (
        REPOSITORY / "sleepagent" / "product_runtime" / "habit_api.py"
    ).read_text(encoding="utf-8")
    diagnostic_cli = (
        REPOSITORY / "tests" / "support" / "radar_cli.py"
    ).read_text(encoding="utf-8")
    canonical_worker = (
        REPOSITORY / "sleepagent" / "product_runtime" / "postgres_worker.py"
    ).read_text(encoding="utf-8")

    assert "product_runner.run" not in diagnostics
    assert "episode_runner.run" not in bridge
    assert "build_product_runtime_bundle_from_env()" not in diagnostics
    assert "build_product_runtime_bundle_from_env()" not in habit
    assert "build_product_runtime_bundle_from_env" not in diagnostic_cli
    assert "_RUNTIME = RadarApiRuntime(" not in diagnostics
    assert "runner.run(request)" in canonical_worker


def test_retired_runtime_test_hooks_are_not_shipped_as_production_bindings() -> None:
    pyproject = (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
    diagnostics = (
        REPOSITORY / "sleepagent" / "product_api" / "diagnostics" / "http.py"
    ).read_text(encoding="utf-8")
    habit = (
        REPOSITORY / "sleepagent" / "product_runtime" / "habit_api.py"
    ).read_text(encoding="utf-8")
    backend_runtime = (
        REPOSITORY / "sleepagent" / "backend_runtime.py"
    ).read_text(encoding="utf-8")

    assert 'radar-agent = "sleepagent.product_runtime.cli:main"' not in pyproject
    assert not any(
        (REPOSITORY / "sleepagent" / "product_runtime" / "cli").glob("*.py")
    )
    assert "def for_test(" not in diagnostics
    assert "reset_radar_api_runtime_for_tests" not in diagnostics
    assert "reset_habit_profile_api_for_tests" not in habit
    assert "reset_active_runtime_for_tests" not in backend_runtime


def test_production_openapi_excludes_demo_and_legacy_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_key = tmp_path / "actor-public.pem"
    actor_key.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    encryption_key = tmp_path / "encryption.key"
    import base64

    encryption_key.write_bytes(base64.b64encode(b"e" * 32))
    service_credential = tmp_path / "service.credential"
    service_credential.write_bytes(b"s" * 32)
    settings = SleepBackendSettings(
        profile="production-live",
        deployment_mode=DeploymentMode.PRODUCTION,
        process_role=ProcessRole.API,
        data_mode=DataMode.LIVE,
        database_dsn="postgresql://api:secret@postgres/sleepagent_live",
        database_identity="sleepagent_live",
        database_role="sleepagent_api_live",
        service_principal_id="sleepagent-api-production",
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:production",),
        enabled_surfaces=frozenset(
            {
                ApiSurface.PUBLIC_V1,
                ApiSurface.PRODUCT,
            }
        ),
        signing_key_ref=f"file:{actor_key}",
        encryption_key_ref=f"file:{encryption_key}",
        service_credential_ref=f"file:{service_credential}",
    )
    runtime = SleepBackendRuntime(
        settings,
        pool=object(),
        uow_factory=object(),
        attestor=lambda: DatabaseAttestation(
            database_identity="sleepagent_live",
            database_role="sleepagent_api_live",
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
        services=build_api_runtime_services(settings, uow_factory=object()),
    )
    paths = set(create_sleep_backend_app(runtime).openapi()["paths"])

    assert any(path.startswith("/api/v1/") for path in paths)
    assert any(path.startswith("/product/sleep/") for path in paths)
    assert not any(path.startswith("/demo/") for path in paths)
    assert not any(path.startswith("/product/radar") for path in paths)
    assert not any(path.startswith("/radar-agent") for path in paths)
    assert paths.isdisjoint({"/health", "/status"})


def test_product_contracts_do_not_retain_generic_projection_placeholder() -> None:
    source = (
        REPOSITORY / "sleepagent" / "product_api" / "contracts.py"
    ).read_text(encoding="utf-8")

    assert "class ProjectionResponse" not in source
    assert "class ProjectionRecord" not in source
    assert "product_projection_response.v1" not in source
