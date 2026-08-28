from __future__ import annotations

import ast
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from reference_client.sleep_api_v1_client import (
    Ed25519ActorSigner,
    HttpResponse,
    SleepApiV1Client,
)


UTC = timezone.utc


class RecordingTransport:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses = list(responses or [{}])

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        payload = self.responses.pop(0)
        return HttpResponse(
            status_code=200,
            headers={},
            body=json.dumps(payload).encode("utf-8"),
        )


def signer() -> Ed25519ActorSigner:
    return Ed25519ActorSigner(
        issuer="issuer",
        audience="audience",
        key_id="key-1",
        actor_id="actor-1",
        subject_id="subject-1",
        role="elder",
        scopes=("product:sleep:today:read", "sleep:reanalysis:write"),
        private_key=ed25519.Ed25519PrivateKey.generate(),
    )


def test_reference_client_is_independent_and_never_models_chat_context() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "reference_client"
        / "sleep_api_v1_client.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any("sleepagent" in ast.unparse(node).lower() for node in imports)
    lowered = source.lower()
    for forbidden in ("chat_context", "conversation_history", "full_chat"):
        assert forbidden not in lowered


def test_report_methods_use_exact_product_paths_without_event_state() -> None:
    transport = RecordingTransport([{}, {}, {}])
    client = SleepApiV1Client(
        base_url="https://sleep.example",
        service_credential="service-secret",
        signer=signer(),
        transport=transport,
        now_factory=lambda: datetime(2026, 8, 27, tzinfo=UTC),
    )

    client.run_report(
        wake_date=date(2026, 8, 26),
        idempotency_key="report-request-1",
    )
    client.get_report(wake_date="2026-08-26", include_trace=True)
    client.list_reports(limit=12, cursor="cursor-1", include_trace=True)

    command, show, listing = transport.requests
    assert command["method"] == "POST"
    assert command["url"] == "https://sleep.example/product/sleep/reports/run"
    assert json.loads(command["body"]) == {
        "schema_version": "product_sleep_report_run.v1",
        "wake_date": "2026-08-26",
    }
    assert command["headers"]["Idempotency-Key"] == "report-request-1"
    assert show["url"] == (
        "https://sleep.example/product/sleep/reports/2026-08-26?trace=true"
    )
    assert listing["url"] == (
        "https://sleep.example/product/sleep/reports?"
        "limit=12&cursor=cursor-1&trace=true"
    )
    assert show["body"] == b""
    assert listing["body"] == b""


def test_event_state_store_is_optional_and_required_only_for_event_polling() -> None:
    client = SleepApiV1Client(
        base_url="https://sleep.example",
        service_credential="service-secret",
        signer=signer(),
        transport=RecordingTransport(),
    )

    with pytest.raises(ValueError, match="event state store"):
        client.poll_events()


def test_report_client_waits_through_pending_until_terminal(monkeypatch) -> None:
    transport = RecordingTransport(
        [
            {"state": "pending"},
            {"state": "ready"},
        ]
    )
    client = SleepApiV1Client(
        base_url="https://sleep.example",
        service_credential="service-secret",
        signer=signer(),
        transport=transport,
    )
    monkeypatch.setattr(
        "reference_client.sleep_api_v1_client.time.sleep",
        lambda _: None,
    )

    report = client.await_report(
        wake_date="2026-08-26",
        timeout_seconds=1,
        interval_seconds=0.01,
    )

    assert report == {"state": "ready"}
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "wake_date",
    ["20260826", "2026-8-26", "2026-02-30", datetime(2026, 8, 26, tzinfo=UTC)],
)
def test_report_client_rejects_noncanonical_wake_dates(wake_date: Any) -> None:
    client = SleepApiV1Client(
        base_url="https://sleep.example",
        service_credential="service-secret",
        signer=signer(),
        transport=RecordingTransport(),
    )

    with pytest.raises(ValueError, match="wake_date"):
        client.get_report(wake_date=wake_date)


def test_private_key_file_requires_absolute_nonsymlink_0600_ed25519(
    tmp_path: Path,
) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_path = tmp_path / "actor-private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)

    loaded = Ed25519ActorSigner.from_private_key_file(
        private_key_path=private_path.resolve(),
        issuer="issuer",
        audience="audience",
        key_id="key-1",
        actor_id="actor-1",
        subject_id="subject-1",
        role="elder",
        scopes=("product:sleep:today:read",),
    )
    assert isinstance(loaded.private_key, ed25519.Ed25519PrivateKey)

    os.chmod(private_path, 0o644)
    with pytest.raises(ValueError, match="0600"):
        Ed25519ActorSigner.from_private_key_file(
            private_key_path=private_path.resolve(),
            issuer="issuer",
            audience="audience",
            key_id="key-1",
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            scopes=("product:sleep:today:read",),
        )

    os.chmod(private_path, 0o600)
    symlink = tmp_path / "actor-link.pem"
    symlink.symlink_to(private_path)
    with pytest.raises(ValueError, match="symlink"):
        Ed25519ActorSigner.from_private_key_file(
            private_key_path=symlink.resolve(strict=False).parent / symlink.name,
            issuer="issuer",
            audience="audience",
            key_id="key-1",
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            scopes=("product:sleep:today:read",),
        )
