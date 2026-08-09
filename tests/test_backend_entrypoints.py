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


pytestmark = pytest.mark.unit
REPOSITORY = Path(__file__).parents[1]


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


def test_configured_backend_main_import_does_not_load_legacy_module() -> None:
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
    "legacy_loaded": "backend.legacy_main" in sys.modules,
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
        SLEEPAGENT_BACKEND_ENABLED_SURFACES="",
        SLEEPAGENT_BACKEND_SIGNING_KEY_REF="test:entrypoint-signing",
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
        "legacy_loaded": "backend.legacy_main" in sys.modules,
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


def test_standalone_sleep_api_lifespan_does_not_own_worker_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone = importlib.import_module("sleepagent.sleep_api.app")
    events: list[str] = []
    monkeypatch.setattr(
        standalone,
        "start_sleep_api_worker",
        lambda: events.append("start"),
    )
    monkeypatch.setattr(
        standalone,
        "stop_sleep_api_worker",
        lambda: events.append("stop"),
    )
    app = standalone.create_sleep_api_app(runtime_provider=lambda: object())

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert events == []

    asyncio.run(exercise())
    assert events == []


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
                ApiSurface.INTERNAL,
            }
        ),
        signing_key_ref=f"file:{actor_key}",
        encryption_key_ref=f"file:{encryption_key}",
        service_credential_ref=f"file:{service_credential}",
        internal_auth_token="production-internal-token-32-bytes",
    )
    runtime = SleepBackendRuntime(
        settings,
        pool=object(),
        uow_factory=object(),
        attestor=lambda: DatabaseAttestation(
            database_identity="sleepagent_live",
            database_role="sleepagent_api_live",
            schema_version=25,
            migrations_clean=True,
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
