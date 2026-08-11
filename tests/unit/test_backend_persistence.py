from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from sleepagent.backend_persistence import (
    PostgresAssertionReplayStore,
    PostgresAuthorityStore,
    PostgresProductBackend,
    PostgresProductIdentityResolver,
)
from sleepagent.product_api.contracts import ProductRole
from sleepagent.product_api.service import ProductApiError, ProductRequestContext
from sleepagent.sleep_api.auth import SleepApiSecurityError
from sleepagent.sleep_api.contracts import PublicErrorCode


pytestmark = pytest.mark.unit
UTC = timezone.utc


class _Cursor:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.statements: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.statements.append((query, params))

    def fetchone(self) -> Any:
        return self.row

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class _Uow(AbstractContextManager["_Uow"]):
    def __init__(self, cursor: _Cursor) -> None:
        self.connection = _Connection(cursor)
        self.commits = 0

    def __enter__(self) -> "_Uow":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1


class _Factory:
    def __init__(self, cursor: _Cursor | None = None, error: Exception | None = None) -> None:
        self.cursor = cursor or _Cursor()
        self.error = error
        self.scopes: list[Any] = []

    def begin(self, scope: Any) -> _Uow:
        self.scopes.append(scope)
        if self.error is not None:
            raise self.error
        return _Uow(self.cursor)


class _SqlError(RuntimeError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _settings() -> Any:
    return SimpleNamespace(
        data_mode=SimpleNamespace(value="replay"),
        service_principal_id="sleepagent-api-test",
    )


def _context(**changes: Any) -> ProductRequestContext:
    values: dict[str, Any] = {
        "service_principal_id": "sleepagent-api-test",
        "actor_id": "actor-1",
        "binding_id": "binding-1",
        "subject_id": "subject-1",
        "role": ProductRole.ELDER,
        "effective_scopes": frozenset({"product:sleep:interaction:write"}),
        "namespace_id": "replay:test",
        "namespace_generation": 1,
        "data_mode": "replay",
        "run_id": "run-1",
        "arm_id": "arm-1",
        "purpose": "sleep_care",
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_epoch": 1,
        "policy_sha256": "a" * 64,
    }
    values.update(changes)
    return ProductRequestContext(**values)


def test_authority_store_distinguishes_denial_from_database_outage() -> None:
    denied = PostgresAuthorityStore(
        _settings(), _Factory(error=_SqlError("P0001"))
    )
    unavailable = PostgresAuthorityStore(
        _settings(), _Factory(error=_SqlError("08006"))
    )

    with pytest.raises(ProductApiError) as denial:
        denied.resolve(
            actor_id="actor-1",
            subject_id="subject-1",
            role=ProductRole.ELDER,
            purpose="sleep_care",
        )
    assert denial.value.status_code == 403
    assert denial.value.retryable is False

    with pytest.raises(ProductApiError) as outage:
        unavailable.resolve(
            actor_id="actor-1",
            subject_id="subject-1",
            role=ProductRole.ELDER,
            purpose="sleep_care",
        )
    assert outage.value.status_code == 503
    assert outage.value.retryable is True


def test_assertion_replay_database_failure_is_fail_closed_and_retryable() -> None:
    store = PostgresAssertionReplayStore(
        _settings(), _Factory(error=_SqlError("08006"))
    )

    with pytest.raises(SleepApiSecurityError) as failure:
        store.consume(
            issuer="issuer",
            assertion_id="assertion-1",
            nonce="nonce-1",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            now=datetime(2029, 1, 1, tzinfo=UTC),
        )

    assert failure.value.code == PublicErrorCode.AUTHORIZATION_UNAVAILABLE
    assert failure.value.status_code == 503
    assert failure.value.retryable is True


def test_product_identity_translates_transport_security_errors() -> None:
    class _Authenticator:
        def verify_identity(self, *args: Any, **kwargs: Any) -> Any:
            raise SleepApiSecurityError(
                PublicErrorCode.INVALID_ACTOR_ASSERTION,
                "bad assertion",
                status_code=401,
            )

    resolver = PostgresProductIdentityResolver(
        authenticator=_Authenticator(),  # type: ignore[arg-type]
        authority=SimpleNamespace(settings=_settings()),  # type: ignore[arg-type]
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/product/sleep/today", "headers": []}
    )

    with pytest.raises(ProductApiError) as failure:
        resolver.resolve(request, body=b"", purpose="sleep_care")

    assert failure.value.code == "invalid_actor_assertion"
    assert failure.value.status_code == 401


def test_command_receipt_reservation_detects_cross_scope_key_reuse() -> None:
    body_sha256 = "b" * 64
    cursor = _Cursor(
        (
            "018f0000-0000-7000-8000-000000000001",
            body_sha256,
            "replay:another",
            "replay",
            "subject-2",
        )
    )
    backend = PostgresProductBackend(_Factory(cursor), cursor_key=b"c" * 32)

    with pytest.raises(ProductApiError) as conflict:
        backend.reserve_command(
            _context(),
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key="same-caller-key",
            body_sha256=body_sha256,
            payload={"message": "hello"},
            target_id=None,
        )

    assert conflict.value.code == "idempotency_scope_conflict"
    advisory_query, advisory_params = cursor.statements[0]
    assert "pg_advisory_xact_lock" in advisory_query
    assert len(advisory_params[0]) == 64
    assert "\x00" not in advisory_params[0]
    receipt_query, params = next(
        (query, params)
        for query, params in cursor.statements
        if "FROM public.backend_command_receipts" in query
    )
    assert "namespace_id = %s" not in receipt_query
    assert len(params) == 4


def test_command_receipt_replay_rejects_an_old_authority_snapshot() -> None:
    body_sha256 = "b" * 64
    cursor = _Cursor(
        (
            "018f0000-0000-7000-8000-000000000001",
            body_sha256,
            "replay:test",
            "replay",
            "subject-1",
            {
                "binding_id": "binding-1",
                "authorization_epoch": 1,
                "privacy_epoch": 1,
                "retrieval_policy_epoch": 1,
                "policy_sha256": "a" * 64,
            },
        )
    )
    backend = PostgresProductBackend(_Factory(cursor), cursor_key=b"c" * 32)

    with pytest.raises(ProductApiError) as conflict:
        backend.reserve_command(
            _context(authorization_epoch=2),
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key="old-authority-key",
            body_sha256=body_sha256,
            payload={"message": "hello"},
            target_id=None,
        )

    assert conflict.value.code == "idempotency_authority_conflict"
    assert conflict.value.status_code == 409


def test_malformed_but_authenticated_cursor_returns_invalid_cursor() -> None:
    backend = PostgresProductBackend(_Factory(), cursor_key=b"d" * 32)
    cursor = backend.cursor_codec.encode(
        {
            "authority": backend._cursor_authority(_context(), kind="today"),
            "generated_at": "not-a-datetime",
            "role_view_id": "view-1",
        }
    )

    with pytest.raises(ProductApiError) as failure:
        backend.list_role_projections(
            _context(), kind="today", limit=20, cursor=cursor
        )

    assert failure.value.code == "invalid_cursor"
    assert failure.value.status_code == 400
