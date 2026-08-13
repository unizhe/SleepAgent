from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sleepagent.persistence.uow import UowScope
from sleepagent.workers.retention import (
    LocalTestRetentionKeyEnvelope,
    PostgresRetentionKeyCoordinator,
    PostgresRawRetentionReader,
    RetentionError,
    RetentionKeyDestroyed,
    encrypt_raw_payload,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:normal-one-night",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def test_local_retention_envelope_round_trips_and_authenticates() -> None:
    envelope = LocalTestRetentionKeyEnvelope(b"k" * 32, key_id="test-kek")
    aad = b"dek-aad"
    data_key = b"d" * 32

    wrapped = envelope.wrap_data_key(data_key, aad=aad)

    assert envelope.unwrap_data_key(wrapped, aad=aad) == data_key
    assert envelope.destroy_wrapped_key(wrapped, aad=aad).startswith(
        "local-envelope:destroy-authorized:"
    )
    with pytest.raises(RetentionError, match="authentication failed"):
        envelope.unwrap_data_key(wrapped, aad=b"wrong")


def test_raw_decrypt_has_stable_key_destroyed_error() -> None:
    envelope = LocalTestRetentionKeyEnvelope(b"k" * 32, key_id="test-kek")
    coordinator = PostgresRetentionKeyCoordinator(envelope)

    with pytest.raises(RetentionKeyDestroyed) as caught:
        coordinator.decrypt_raw(
            scope=_scope(),
            generation=1,
            wrapped_data_key=None,
            dek_sha256="0" * 64,
            status="shredded",
            encrypted_payload=b"unreadable",
            aad=b"raw-aad",
        )

    assert caught.value.code == "key_destroyed"
    assert str(caught.value) == "key_destroyed"


def test_raw_payload_uses_unwrapped_domain_key_not_kek() -> None:
    envelope = LocalTestRetentionKeyEnvelope(b"k" * 32, key_id="test-kek")
    coordinator = PostgresRetentionKeyCoordinator(envelope)
    scope = _scope()
    data_key = b"d" * 32
    generation = 1
    dek_aad = (
        b'{"data_mode":"replay","generation":1,'
        b'"namespace_id":"replay:normal-one-night",'
        b'"retention_domain":"raw","schema_version":"retention_dek_aad.v1",'
        b'"subject_id":"subject-1"}'
    )
    wrapped = envelope.wrap_data_key(data_key, aad=dek_aad)
    encrypted = encrypt_raw_payload(data_key, b'{"night":"proof"}', aad=b"raw-aad")

    assert coordinator.decrypt_raw(
        scope=scope,
        generation=generation,
        wrapped_data_key=wrapped,
        dek_sha256=(
            "fbbbb6de2aa74c3c9570d2d8db1de31eadb66113c96034a7adb21243754d7683"
        ),
        status="active",
        encrypted_payload=encrypted,
        aad=b"raw-aad",
    ) == b'{"night":"proof"}'


class _Cursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        assert "raw.encryption_protocol_version >= 2" in query
        assert params[0] == "raw-1"

    def fetchone(self) -> tuple[object, ...]:
        return self.row

    def close(self) -> None:
        return None


class _Uow:
    def __init__(self, cursor: _Cursor) -> None:
        self.connection = type("Connection", (), {"cursor": lambda _self: cursor})()
        self.committed = False

    def __enter__(self) -> "_Uow":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.uow = _Uow(_Cursor(row))

    def begin(self, scope: UowScope) -> _Uow:
        assert scope == _scope()
        return self.uow


def test_canonical_raw_reader_reports_key_destroyed_after_shred() -> None:
    coordinator = PostgresRetentionKeyCoordinator(
        LocalTestRetentionKeyEnvelope(b"k" * 32, key_id="test-kek")
    )
    reader = PostgresRawRetentionReader(
        _Factory((b"ciphertext", "0" * 64, 1, None, "a" * 64, "shredded")),
        keys=coordinator,
    )

    with pytest.raises(RetentionKeyDestroyed, match="key_destroyed"):
        reader.read(_scope(), raw_ingress_record_id="raw-1")
