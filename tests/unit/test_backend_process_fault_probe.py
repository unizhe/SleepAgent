from __future__ import annotations

from pathlib import Path

import pytest

from scripts.backend_process_fault_probe import (
    ADMIN_DSN_ENV,
    WORKER_DSN_ENV,
    TerminalProbeError,
    _dsn,
    _wait_until,
    build_parser,
    delivery_lock_name,
)


pytestmark = pytest.mark.unit


def test_fault_probe_uses_canonical_delivery_lock_name() -> None:
    assert delivery_lock_name("effect-1") == (
        "sleepagent:replay-delivery-effect:effect-1"
    )
    with pytest.raises(ValueError, match="semantic effect key"):
        delivery_lock_name("")


def test_fault_probe_never_accepts_database_credentials_on_argv() -> None:
    help_text = build_parser().format_help()
    assert "--dsn" not in help_text
    assert ADMIN_DSN_ENV not in help_text
    assert WORKER_DSN_ENV not in help_text


def test_fault_probe_reads_each_database_role_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (ADMIN_DSN_ENV, WORKER_DSN_ENV):
        monkeypatch.delenv(name, raising=False)
        with pytest.raises(RuntimeError, match=name):
            _dsn(name)
        monkeypatch.setenv(name, f"postgresql://{name.lower()}/replay")
        assert _dsn(name).endswith("/replay")


def test_wait_until_tolerates_restart_but_not_lost_fault_window() -> None:
    outcomes: list[object] = [ConnectionError("restart"), None, {"ready": True}]

    def transient_probe() -> object | None:
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    assert _wait_until(
        transient_probe,
        timeout_seconds=1,
        description="test state",
    ) == {"ready": True}

    with pytest.raises(TerminalProbeError, match="already terminal"):
        _wait_until(
            lambda: (_ for _ in ()).throw(TerminalProbeError("already terminal")),
            timeout_seconds=1,
            description="test terminal state",
        )


def test_fault_process_orchestration_is_single_canonical_entrypoint() -> None:
    script = Path("scripts/verify_backend.sh").read_text(encoding="utf-8")
    assert "fault-process" in script
    assert "kill -s SIGKILL worker" in script
    assert "hold-product-commit" in script
    assert "restart postgres" in script
    assert "wait-delivery-send-started" in script
    assert not list(Path("scripts").glob("verify_backend_stage*.sh"))
