from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sleepagent.app import BoundedRequestMiddleware, create_sleep_backend_app
from sleepagent.config import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.domain.postgres_slice import RawPayloadCipher
from sleepagent.integrations.perceptor import ingestion as ingestion_module
from sleepagent.integrations.perceptor.ingestion import (
    WEBHOOK_PATH,
    PerceptorWebhookError,
    PerceptorWebhookService,
    create_perceptor_router,
)
from sleepagent.integrations.perceptor.reconciliation import (
    PerceptorReconciliationResult,
)
from sleepagent.integrations.perceptor.signing import (
    PUSH_SIGNING_KEY_MODE,
    PUSH_SIGNING_PATH,
    sign_parameters,
)
from sleepagent.persistence.uow import ExternalIngressScope
from sleepagent.process import RuntimeServices, SleepBackendRuntime


UTC = timezone.utc
NOW = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
SECRET = "synthetic-production-push-secret"
SANITIZED_RECORDED_REAL = (
    Path(__file__).parents[1]
    / "fixtures"
    / "perceptor_v2_5_2"
    / "sanitized_recorded_real_vital.json"
)


def test_later_push_emits_pull_symmetric_overlap_and_conflict_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        ingestion_module,
        "record_backend_signal",
        lambda *, category, outcome: signals.append((category, outcome)),
    )
    monkeypatch.setattr(
        ingestion_module,
        "log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    reconciliations = (
        PerceptorReconciliationResult(
            canonical_observation_id="observation-overlap",
            canonical_created=False,
            acquisition_created=True,
            push_pull_overlap=True,
            conflict_created_count=0,
            duplicate=False,
        ),
        PerceptorReconciliationResult(
            canonical_observation_id="observation-conflict",
            canonical_created=True,
            acquisition_created=True,
            push_pull_overlap=False,
            conflict_created_count=2,
            duplicate=False,
        ),
    )

    ingestion_module._emit_push_reconciliation_observability(reconciliations)

    assert signals == [
        ("reconciliation", "overlap"),
        ("reconciliation", "conflict"),
    ]
    assert events == [
        ("push_pull_overlap", {"endpoint": WEBHOOK_PATH, "count": 1}),
        ("push_pull_conflict", {"endpoint": WEBHOOK_PATH, "count": 2}),
    ]


class Cursor:
    rowcount = 1

    def __init__(self, row: tuple[Any, ...] | None, error: Exception | None) -> None:
        self.row = row
        self.error = error
        self.statement = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, statement: str, params: tuple[Any, ...]) -> None:
        if self.error is not None:
            raise self.error
        self.statement = statement
        self.params = params

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


class Uow:
    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor
        self.connection = self
        self.committed = False

    def cursor(self) -> Cursor:
        return self._cursor

    def __enter__(self) -> "Uow":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True


class UowFactory:
    def __init__(
        self,
        row: tuple[Any, ...] | None = (
            "accepted",
            "raw-committed",
            "work-committed",
            "subject-1",
            "binding-1",
            False,
        ),
        *,
        error: Exception | None = None,
    ) -> None:
        self.cursor = Cursor(row, error)
        self.uow = Uow(self.cursor)
        self.scopes: list[ExternalIngressScope] = []

    def begin(self, scope: ExternalIngressScope) -> Uow:
        self.scopes.append(scope)
        return self.uow


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, _now: datetime | None = None) -> str:
        self.value += 1
        return f"generated-{self.value}"


def _settings() -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="test-live-perceptor",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.API,
        data_mode=DataMode.LIVE,
        database_dsn="postgresql://api:secret@postgres/live_db",
        database_identity="live_db",
        database_role="sleepagent_api_live",
        service_principal_id="sleepagent-perceptor-api-test",
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:perceptor-test",),
        enabled_surfaces=frozenset({ApiSurface.PERCEPTOR_PUSH}),
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
        perceptor_client_secret_ref="test:perceptor-secret",
        perceptor_provider_account_id="perceptor-account-test",
        perceptor_namespace_id="live:perceptor-test",
        perceptor_namespace_generation=1,
        perceptor_authorization_epoch=1,
        perceptor_freshness_seconds=300,
        raw_retention_seconds=3600,
    )


def _payload(
    *,
    timestamp: datetime = NOW,
    event_type: str = "VitalSignsDataEvent",
    message_id: str = "synthetic-message-1",
) -> dict[str, object]:
    data = (
        '{"HeartRate":70,"BreathRate":16,"BodyShake":1,"OnBed":1,'
        f'"ReportTime":"{int(timestamp.timestamp() * 1000)}",'
        '"DateTime":"2026-08-21T10:00:00.000"}'
    )
    if event_type != "VitalSignsDataEvent":
        data = '{"FutureField":"opaque"}'
    return {
        "client_id": "synthetic-client",
        "version": "2.0",
        "timestamp": int(timestamp.timestamp()),
        "sign_version": "2.0",
        "sign_nonce": "synthetic-nonce-1",
        "sign_method": "HMAC-SHA1",
        "message_id": message_id,
        "product_id": 7000000000000000001,
        "device_id": 7000000000000000002,
        "device_name": "SYNTHETIC-BOUND-DEVICE",
        "home_id": 7000000000000000003,
        "type": event_type,
        "data": data,
    }


def _signed_raw(**kwargs: Any) -> bytes:
    payload = _payload(**kwargs)
    payload["sign"] = sign_parameters(
        payload,
        client_secret=SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )
    return json.dumps(payload, separators=(",", ":")).encode()


def _service(factory: UowFactory) -> PerceptorWebhookService:
    return PerceptorWebhookService(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_secret=SECRET,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-raw-key"),
        now_factory=lambda: NOW,
        id_generator=Ids(),
    )


def test_valid_signed_push_preserves_exact_bytes_and_commits_once() -> None:
    factory = UowFactory()
    raw = _signed_raw()

    result = _service(factory).accept(
        raw,
        content_type="application/json;charset=UTF-8",
        request_path=WEBHOOK_PATH,
    )

    assert result.disposition == "accepted"
    assert factory.uow.committed is True
    assert factory.scopes == [
        ExternalIngressScope(
            namespace_id="live:perceptor-test",
            namespace_generation=1,
            service_principal_id="sleepagent-perceptor-api-test",
            authorization_epoch=1,
        )
    ]
    params = factory.cursor.params
    assert len(params) == 26
    assert params[11] == hashlib.sha256(raw).hexdigest()
    assert params[12] != raw
    assert params[15] == len(raw)
    assert params[18] is True
    assert params[19] is True
    assert "synthetic-production-push-secret" not in repr(params)


def test_push_raw_idempotency_identity_is_generation_scoped() -> None:
    raw = _signed_raw()
    identities: list[str] = []
    for generation in (1, 2):
        factory = UowFactory()
        service = PerceptorWebhookService(
            _settings().model_copy(
                update={"perceptor_namespace_generation": generation}
            ),
            factory,  # type: ignore[arg-type]
            client_secret=SECRET,
            cipher=RawPayloadCipher(b"k" * 32, key_id="test-raw-key"),
            now_factory=lambda: NOW,
            id_generator=Ids(),
        )
        service.accept(
            raw,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        identities.append(str(factory.cursor.params[10]))

    assert identities[0] != identities[1]
    assert all(value.startswith("perceptor-push-generation.v2:") for value in identities)


def test_sanitized_recorded_real_shape_traverses_production_endpoint() -> None:
    payload = json.loads(SANITIZED_RECORDED_REAL.read_bytes())
    payload["timestamp"] = int(NOW.timestamp())
    payload["sign"] = sign_parameters(
        payload,
        client_secret=SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )
    raw = json.dumps(payload, separators=(",", ":")).encode()
    factory = UowFactory()
    app = FastAPI()
    app.include_router(create_perceptor_router(_service(factory)))
    app.add_middleware(
        BoundedRequestMiddleware,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=16,
        max_json_members=200,
        request_timeout_seconds=2,
        data_mode="live",
    )

    response = TestClient(app).request(
        "POST",
        WEBHOOK_PATH,
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    assert factory.uow.committed is True
    assert factory.cursor.params[18] is True
    assert factory.cursor.params[19] is True


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            ("duplicate", "raw-1", "work-1", "subject-1", "binding-1", True),
            (True, "duplicate"),
        ),
        (
            ("quarantined_unbound", "raw-2", None, None, None, False),
            (False, "quarantined_unbound"),
        ),
        (
            ("quarantined_unknown", "raw-3", None, None, None, False),
            (False, "quarantined_unknown"),
        ),
    ],
)
def test_durable_dispositions_are_acknowledgeable(
    row: tuple[Any, ...], expected: tuple[bool, str]
) -> None:
    result = _service(UowFactory(row)).accept(
        _signed_raw(
            event_type=(
                "FutureVendorEvent"
                if row[0] == "quarantined_unknown"
                else "VitalSignsDataEvent"
            )
        ),
        content_type="application/json",
        request_path=WEBHOOK_PATH,
    )
    assert (result.duplicate, result.disposition) == expected


def test_stale_new_request_fails_closed_after_authenticated_classification() -> None:
    factory = UowFactory(("stale", None, None, None, None, False))
    with pytest.raises(PerceptorWebhookError, match="stale_request"):
        _service(factory).accept(
            _signed_raw(timestamp=NOW - timedelta(hours=1)),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
    assert factory.uow.committed is True


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{", "invalid_push_contract"),
        (b'{"client_id":"one","client_id":"two"}', "invalid_push_contract"),
    ],
)
def test_malformed_and_duplicate_json_never_reach_database(
    raw: bytes, code: str
) -> None:
    factory = UowFactory()
    with pytest.raises(PerceptorWebhookError, match=code):
        _service(factory).accept(
            raw,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
    assert factory.scopes == []


def test_bad_and_missing_signature_never_reach_database() -> None:
    for mutation in ("bad", "missing"):
        payload = _payload()
        if mutation == "bad":
            payload["sign"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
        raw = json.dumps(payload, separators=(",", ":")).encode()
        factory = UowFactory()
        with pytest.raises(PerceptorWebhookError):
            _service(factory).accept(
                raw,
                content_type="application/json",
                request_path=WEBHOOK_PATH,
            )
        assert factory.scopes == []


def test_db_failure_returns_non_success_and_no_false_ack() -> None:
    service = _service(UowFactory(error=RuntimeError("database unavailable")))
    app = FastAPI()
    app.include_router(create_perceptor_router(service))
    app.add_middleware(
        BoundedRequestMiddleware,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=16,
        max_json_members=200,
        request_timeout_seconds=2,
        data_mode="live",
    )
    response = TestClient(app).request(
        "POST",
        WEBHOOK_PATH,
        content=_signed_raw(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 503
    assert response.json()["success"] is False
    assert response.json()["retryable"] is True


def test_production_router_has_only_the_authoritative_perceptor_path() -> None:
    app = FastAPI()
    app.include_router(create_perceptor_router(_service(UowFactory())))
    paths = {route.path for route in app.routes}
    assert WEBHOOK_PATH in paths
    assert "/receive" not in paths


def test_composition_root_mounts_perceptor_without_worker_handlers() -> None:
    factory = UowFactory()
    service = _service(factory)
    runtime = SleepBackendRuntime(
        _settings(),
        pool=object(),  # type: ignore[arg-type]
        uow_factory=factory,
        attestor=lambda: None,  # type: ignore[arg-type,return-value]
        services=RuntimeServices(perceptor_push=service),
    )

    app = create_sleep_backend_app(runtime)

    paths = {route.path for route in app.routes}
    assert WEBHOOK_PATH in paths
    assert "/receive" not in paths
    assert runtime.worker_handlers == {}


def test_compressed_transport_is_rejected_before_signature_verification() -> None:
    factory = UowFactory()
    app = FastAPI()
    app.include_router(create_perceptor_router(_service(factory)))
    app.add_middleware(
        BoundedRequestMiddleware,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=16,
        max_json_members=200,
        request_timeout_seconds=2,
        data_mode="live",
    )

    response = TestClient(app).request(
        "POST",
        WEBHOOK_PATH,
        content=gzip.compress(_signed_raw()),
        headers={
            "content-type": "application/json",
            "content-encoding": "gzip",
        },
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_content_encoding"
    assert factory.scopes == []
