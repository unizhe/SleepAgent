from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
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


class _SequenceCursor(_Cursor):
    def __init__(self, rows: list[Any]) -> None:
        super().__init__()
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows.pop(0)


class _RowsCursor(_Cursor):
    def __init__(self, rows: list[Any]) -> None:
        super().__init__()
        self.rows = rows

    def fetchall(self) -> list[Any]:
        return list(self.rows)


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


def test_product_identity_policy_pin_is_stable_across_asserted_scope_subsets() -> None:
    class Authenticator:
        asserted_scope = "product:sleep:interaction:write"

        def verify_identity(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(
                service_principal=SimpleNamespace(
                    principal_id="sleepagent-api-test"
                ),
                claims=SimpleNamespace(
                    actor_id="actor-1",
                    subject_id="subject-1",
                    role=SimpleNamespace(value="elder"),
                    scope=(self.asserted_scope,),
                    authorization_epoch=1,
                    privacy_epoch=1,
                    retrieval_policy_epoch=1,
                ),
            )

    class Authority:
        settings = _settings()

        def resolve(self, **kwargs: Any) -> Any:
            del kwargs
            return SimpleNamespace(
                namespace_id="replay:test",
                namespace_generation=1,
                data_mode="replay",
                run_id="run-1",
                arm_id="arm-1",
                binding_id="binding-1",
                role=ProductRole.ELDER,
                effective_scopes=frozenset(
                    {
                        "product:sleep:interaction:write",
                        "product:sleep:interaction:answer",
                    }
                ),
                authorization_epoch=1,
                privacy_epoch=1,
                retrieval_policy_epoch=1,
            )

    authenticator = Authenticator()
    resolver = PostgresProductIdentityResolver(
        authenticator=authenticator,  # type: ignore[arg-type]
        authority=Authority(),  # type: ignore[arg-type]
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/product/sleep", "headers": []}
    )

    start = resolver.resolve(request, body=b"", purpose="sleep_care")
    authenticator.asserted_scope = "product:sleep:interaction:answer"
    answer = resolver.resolve(request, body=b"", purpose="sleep_care")

    assert start.effective_scopes == frozenset(
        {"product:sleep:interaction:write"}
    )
    assert answer.effective_scopes == frozenset(
        {"product:sleep:interaction:answer"}
    )
    assert start.policy_sha256 == answer.policy_sha256


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


def test_interaction_reservation_routes_to_real_handler_without_early_handle_consume() -> None:
    cursor = _SequenceCursor([None, None])
    backend = PostgresProductBackend(
        _Factory(cursor),
        cursor_key=b"c" * 32,
        id_generator=lambda _at=None: "01987654-3210-7abc-8def-0123456789ab",
    )

    operation_id = backend.reserve_command(
        _context(),
        route_template="/product/sleep/interactions/{interaction_id}/answer",
        command_type="interaction.answer",
        idempotency_key="answer-one",
        body_sha256="b" * 64,
        payload={"answer_handle": "server-owned", "answer": "restless"},
        target_id="interaction-1",
    )

    assert operation_id == "01987654-3210-7abc-8def-0123456789ab"
    operation_insert, params = next(
        (query, params)
        for query, params in cursor.statements
        if "INSERT INTO public.sleep_domain_operations" in query
    )
    assert "queue_name" in operation_insert
    assert params[-3] == "product_interaction"
    assert all(
        "sleepagent_consume_pending_handle" not in query
        for query, _params in cursor.statements
    )


def test_today_reads_only_current_policy_pinned_typed_projection() -> None:
    committed_at = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {
        "schema_version": "product_sleep_today.v1",
        "data_mode": "replay",
        "synthetic_non_release": True,
        "state": "ready",
        "subject_ref": "subject-1",
        "role": "elder",
        "episode_id": "episode-1",
        "episode_revision_id": "episode-revision-1",
        "episode_local_date": "2026-08-07",
        "assignment_basis": "observed_wake",
        "analysis_revision_id": "analysis-1",
        "projection_id": "projection-1",
        "projection_version": 1,
        "committed_at": committed_at.isoformat(),
        "content": {
            "audience": "elder",
            "summary_text": "Sleep summary is ready.",
            "context_notice": "Latest completed night.",
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cursor = _Cursor((payload, digest, committed_at))
    backend = PostgresProductBackend(_Factory(cursor), cursor_key=b"e" * 32)

    result = backend.get_today_projection(_context())

    assert result is not None
    assert result.analysis_revision_id == "analysis-1"
    query, params = cursor.statements[0]
    assert "public_schema_version" in query
    assert "NOT EXISTS" in query
    assert "newer.revision_number > analysis.revision_number" in query
    assert "view.policy_sha256" not in query
    assert params[-1] == 1


def test_today_treats_legacy_projection_as_no_data_without_backfill() -> None:
    cursor = _Cursor(None)
    backend = PostgresProductBackend(_Factory(cursor), cursor_key=b"f" * 32)

    assert backend.get_today_projection(_context()) is None
    assert "public_schema_version" in cursor.statements[0][0]


def test_interaction_status_exposes_only_committed_waiting_handle() -> None:
    updated_at = datetime(2026, 8, 11, tzinfo=UTC)
    cursor = _Cursor(
        (
            "succeeded",
            {
                "schema_version": "backend_operation.v2",
                "interaction_id": "interaction-1",
                "result": {
                    "schema_version": "product_interaction_result.v1",
                    "result_ref": "interaction-1",
                    "interaction_revision": 1,
                    "interaction_state": "waiting_user",
                    "public_state": "waiting_for_input",
                    "answer_handle": "opaque-server-handle",
                },
            },
            "succeeded",
            updated_at,
        )
    )
    backend = PostgresProductBackend(_Factory(cursor), cursor_key=b"g" * 32)

    result = backend.get_operation(_context(), operation_id="operation-1")

    assert result is not None
    assert result.state.value == "waiting_for_input"
    assert result.interaction_id == "interaction-1"
    assert result.interaction_revision == 1
    assert result.answer_handle == "opaque-server-handle"


def test_dedicated_stage_three_reads_do_not_reuse_generic_view_content() -> None:
    committed_at = datetime(2026, 8, 11, tzinfo=UTC)
    trends_cursor = _RowsCursor(
        [
            (
                "episode-1",
                "episode-revision-1",
                committed_at.date(),
                "observed_wake",
                "analysis-1",
                "projection-1",
                "ready",
                480,
                committed_at,
            )
        ]
    )
    trends_backend = PostgresProductBackend(
        _Factory(trends_cursor), cursor_key=b"h" * 32
    )

    trends = trends_backend.get_trends(_context(), limit=10, cursor=None)

    assert trends.schema_version == "product_sleep_trends.v1"
    assert trends.items[0].sleep_window_minutes == 480
    assert "view.view_json" not in trends_cursor.statements[0][0]
    assert "view.policy_sha256" not in trends_cursor.statements[0][0]
    assert "newer.revision_number" in trends_cursor.statements[0][0]

    records_cursor = _RowsCursor(
        [
            (
                "analysis-2",
                2,
                "analysis-1",
                "episode-1",
                "episode-revision-1",
                "projection-2",
                2,
                "ready",
                True,
                committed_at,
            )
        ]
    )
    records_backend = PostgresProductBackend(
        _Factory(records_cursor), cursor_key=b"i" * 32
    )

    records = records_backend.get_records(_context(), limit=10, cursor=None)

    assert records.schema_version == "product_sleep_records.v1"
    assert records.items[0].parent_analysis_revision_id == "analysis-1"
    assert records.items[0].is_current is True
    assert "view.view_json" not in records_cursor.statements[0][0]
    assert "view.policy_sha256" not in records_cursor.statements[0][0]

    care_cursor = _RowsCursor(
        [
            (
                "care_action",
                "care-1",
                "interaction-1",
                "confirmed_pending_delivery",
                "sleep_hygiene_followup",
                None,
                committed_at,
                committed_at,
                None,
                committed_at,
            ),
            (
                "care_followup",
                "care-followup:episode-1",
                None,
                "pending_feedback",
                None,
                None,
                None,
                committed_at,
                "episode-1",
                committed_at,
            )
        ]
    )
    care_backend = PostgresProductBackend(
        _Factory(care_cursor), cursor_key=b"j" * 32
    )

    care = care_backend.get_care(_context(), limit=10, cursor=None)

    assert care.schema_version == "product_sleep_care.v1"
    assert care.items[0].action_kind == "sleep_hygiene_followup"
    assert care.items[0].record_type == "care_action"
    assert care.items[1].record_type == "care_followup"
    assert care.items[1].night_episode_id == "episode-1"
    query = care_cursor.statements[0][0]
    assert "decision.choice = 'confirm'" in query
    assert "decision.authorization_epoch" in query


def test_stage_three_cursor_fails_closed_after_authority_epoch_drift() -> None:
    backend = PostgresProductBackend(_Factory(), cursor_key=b"k" * 32)
    original = _context()
    cursor = backend.cursor_codec.encode(
        {
            "kind": "trends",
            "authority": backend._cursor_authority(original, kind="trends"),
            "sort_at": datetime(2026, 8, 11, tzinfo=UTC).isoformat(),
            "item_id": "projection-1",
        }
    )

    with pytest.raises(ProductApiError) as failure:
        backend.get_trends(
            _context(privacy_epoch=2),
            limit=10,
            cursor=cursor,
        )

    assert failure.value.code == "cursor_resync_required"
    assert failure.value.status_code == 409
