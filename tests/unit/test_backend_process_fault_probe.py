from __future__ import annotations

import pytest

from scripts.backend_process_fault_probe import (
    DSN_ENV,
    TerminalProbeError,
    _dsn,
    _wait_until,
    build_parser,
    delivery_lock_name,
)


pytestmark = pytest.mark.unit


def test_delivery_fault_lock_matches_deterministic_sink_boundary() -> None:
    assert delivery_lock_name("effect-key-1") == (
        "sleepagent:replay-delivery-effect:effect-key-1"
    )
    with pytest.raises(ValueError, match="semantic effect key"):
        delivery_lock_name("")


def test_fault_probe_never_accepts_database_credentials_on_argv() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert "--dsn" not in help_text
    assert DSN_ENV not in help_text


def test_fault_probe_reads_dsn_only_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=DSN_ENV):
        _dsn()
    monkeypatch.setenv(DSN_ENV, "postgresql://fault-probe:test@db/replay")
    assert _dsn() == "postgresql://fault-probe:test@db/replay"


def test_wait_until_tolerates_transient_probe_failure() -> None:
    outcomes: list[object] = [ConnectionError("restart"), None, {"ready": True}]

    def probe() -> object | None:
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    assert _wait_until(
        probe,
        timeout_seconds=1,
        description="test state",
    ) == {"ready": True}


def test_wait_until_does_not_hide_terminal_fault_window() -> None:
    def probe() -> None:
        raise TerminalProbeError("already terminal")

    with pytest.raises(TerminalProbeError, match="already terminal"):
        _wait_until(
            probe,
            timeout_seconds=1,
            description="test terminal state",
        )
