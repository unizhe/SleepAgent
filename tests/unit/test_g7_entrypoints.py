"""Regression coverage for the concrete G7 command entrypoints."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from sleepagent.bootstrap import scheduler
from sleepagent.config import ProcessRole
from sleepagent import device_cli


class _Pool:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True


def test_device_cli_uses_provider_lifecycle_contract(monkeypatch) -> None:
    pool = _Pool()
    settings = SimpleNamespace(
        process_role=ProcessRole.API,
        data_mode=SimpleNamespace(value="live"),
        service_principal_id="api-principal",
        database_dsn=SecretStr("postgresql://api@localhost/sleepagent"),
        pool_min_size=1,
        pool_max_size=1,
        pool_timeout_seconds=5.0,
        lock_timeout_ms=2_000,
        statement_timeout_ms=15_000,
        idle_transaction_timeout_ms=10_000,
    )
    monkeypatch.setattr(
        device_cli.SleepBackendSettings,
        "from_environment",
        lambda: settings,
    )
    monkeypatch.setattr(
        device_cli.PsycopgPoolProvider,
        "from_dsn",
        lambda *args, **kwargs: pool,
    )
    monkeypatch.setattr(device_cli, "_execute", lambda *args, **kwargs: ())

    result = device_cli.main(
        [
            "--namespace-id",
            "live:acceptance",
            "--namespace-generation",
            "1",
            "--subject-id",
            "subject-1",
            "--actor-id",
            "actor-1",
            "--actor-role",
            "elder",
            "--authorization-id",
            "authorization-1",
            "--authorization-epoch",
            "1",
            "--privacy-epoch",
            "1",
            "--retrieval-policy-epoch",
            "1",
            "schedule-list",
        ]
    )

    assert result == 0
    assert pool.opened is True
    assert pool.closed is True


def test_scheduler_cli_uses_provider_lifecycle_contract(monkeypatch) -> None:
    pool = _Pool()
    due = SimpleNamespace(fire_due=lambda *, limit: ())
    settings = SimpleNamespace(acquisition_scheduler_enabled=True)
    monkeypatch.setattr(
        scheduler.SleepBackendSettings,
        "from_environment",
        lambda: settings,
    )
    monkeypatch.setattr(
        scheduler,
        "build_scheduler",
        lambda settings, worker_instance: (pool, due),
    )

    result = scheduler.main(["once", "--limit", "1"])

    assert result == 0
    assert pool.opened is True
    assert pool.closed is True


def test_test_role_bootstrap_grants_scheduled_pull_functions_to_worker() -> None:
    source = Path("sleepagent/persistence/migrate.py").read_text(encoding="utf-8")
    worker_functions = source.split("worker_functions = (", 1)[1].split(
        "demo_functions = (", 1
    )[0]

    assert "sleepagent_ingest_perceptor_pull(" in worker_functions
    assert "sleepagent_plan_perceptor_history(" in worker_functions
