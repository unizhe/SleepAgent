"""Test-only runtime composition helpers for legacy contract suites."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, cast

from fastapi import FastAPI

import sleepagent.backend_runtime as backend_runtime_module
import sleepagent.product_api.diagnostics.http as diagnostic_http_module
from sleepagent.backend_app import create_sleep_backend_app
from sleepagent.backend_runtime import RuntimeServices
from sleepagent.backend_settings import ApiSurface, DataMode, ProcessRole
from sleepagent.persistence import RadarPersistenceStore
from sleepagent.product_api.diagnostics.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    RadarApiRuntime,
)
from sleepagent.product_device import PRODUCT_RADAR_API_KEY_ENV
from sleepagent.product_runtime.habit_api import (
    PRODUCT_API_KEY_ENV,
    configure_habit_profile_runtime,
)
from sleepagent.product_runtime.runtime_contracts import ProductEpisodeRunResult
from sleepagent.product_runtime.runtime_factory import (
    ProductRuntimeBundle,
    build_product_runtime_bundle_from_env,
)
from sleepagent.product_runtime.task_runtime import InvalidTaskTransition
from sleepagent.sleep_api.router import RuntimeProvider


def reset_backend_runtime_state() -> None:
    """Reset the process singleton guard between isolated runtime tests."""

    with backend_runtime_module._ACTIVE_LOCK:
        backend_runtime_module._ACTIVE_RUNTIME_ID = None


@dataclass
class _CompatibilityRuntime:
    provider: RuntimeProvider

    def __post_init__(self) -> None:
        self.settings = SimpleNamespace(
            process_role=ProcessRole.API,
            enabled_surfaces=frozenset({ApiSurface.PUBLIC_V1}),
            data_mode=DataMode.LIVE,
            max_compressed_body_bytes=1_048_576,
            max_decompressed_body_bytes=2_097_152,
            max_json_depth=32,
            max_json_members=20_000,
            request_timeout_seconds=15.0,
        )
        self.services = RuntimeServices(public_v1_runtime_provider=self.provider)
        self.worker_handlers: dict[str, Any] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def create_test_sleep_api_app(runtime_provider: RuntimeProvider) -> FastAPI:
    return create_sleep_backend_app(
        _CompatibilityRuntime(runtime_provider),  # type: ignore[arg-type]
        enabled_surfaces={ApiSurface.PUBLIC_V1},
    )


def build_test_radar_runtime(
    *,
    product_runtime: ProductRuntimeBundle,
    development_mode: bool = True,
) -> RadarApiRuntime:
    runner = product_runtime.runner

    def required_command(name: str) -> Callable[[Any], ProductEpisodeRunResult]:
        command = getattr(runner, name, None)
        if command is not None:
            return cast(Callable[[Any], ProductEpisodeRunResult], command)

        def unavailable(_command: Any) -> ProductEpisodeRunResult:
            raise InvalidTaskTransition(
                f"retired diagnostic runner does not support {name}"
            )

        return unavailable

    return RadarApiRuntime(
        product_runtime=product_runtime,
        execute_episode=required_command("run"),
        commit_confirmations=required_command("commit_frozen_confirmations"),
        reexecute_episode=required_command("reexecute_with_added_fact"),
        development_mode=development_mode,
    )


def configure_test_radar_runtime(
    connection: sqlite3.Connection | None = None,
    *,
    product_runtime: ProductRuntimeBundle | None = None,
) -> RadarApiRuntime:
    if product_runtime is not None and connection is not None:
        raise ValueError("an injected Product runtime owns the API persistence store")
    resolved_runtime = product_runtime
    if resolved_runtime is None:
        persistence = RadarPersistenceStore.connect_sqlite(
            connection or sqlite3.connect(":memory:", check_same_thread=False)
        )
        resolved_runtime = build_product_runtime_bundle_from_env(
            persistence_store=persistence,
        )
    runtime = build_test_radar_runtime(
        product_runtime=resolved_runtime,
        development_mode=(
            os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() == "true"
        ),
    )
    api_key = (
        os.getenv(RADAR_AGENT_API_KEY_ENV)
        or os.getenv(PRODUCT_RADAR_API_KEY_ENV)
    )
    diagnostic_http_module.get_radar_api_runtime = lambda: runtime
    diagnostic_http_module._radar_api_key = lambda: api_key
    configure_habit_profile_runtime(runtime.product_runtime)
    return runtime


def configure_test_habit_profile_runtime(
    connection: sqlite3.Connection | None = None,
) -> None:
    test_connection = connection or sqlite3.connect(
        ":memory:",
        check_same_thread=False,
    )
    persistence = RadarPersistenceStore.connect_sqlite(test_connection)
    configure_habit_profile_runtime(
        build_product_runtime_bundle_from_env(persistence_store=persistence),
        api_key=os.getenv(PRODUCT_API_KEY_ENV),
    )
