"""Deployment entry point for the canonical modular SleepAgent backend."""

from __future__ import annotations

from sleepagent.backend_app import create_sleep_backend_app
from sleepagent.backend_runtime import build_backend_runtime
from sleepagent.backend_settings import ProcessRole, SleepBackendSettings


settings = SleepBackendSettings.from_environment()
if settings.process_role != ProcessRole.API:
    raise RuntimeError("backend.main requires PROCESS_ROLE=api")
runtime = build_backend_runtime(settings)
app = create_sleep_backend_app(runtime)


__all__ = ["app"]
