from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from sleepagent.config import (
    ApiSurface,
    DataMode as ConfigDataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.domain.contracts import (
    DataMode,
    DeviceBinding,
    DeviceBindingStatus,
    ObservationType,
    ProviderDeviceIdentity,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    NormalizationLease,
    RawPayloadCipher,
    SleepSliceInvariantError,
)
from sleepagent.integrations.perceptor.client import (
    GET_CURRENT_ENDPOINT,
    HISTORY_ENDPOINT,
    REALTIME_READ_ENDPOINT,
    SLEEP_REPORT_ENDPOINT,
    PlatformEvidencedRead,
    PlatformRawResponseEvidence,
)
from sleepagent.integrations.perceptor.pull import PullContractError
from sleepagent.integrations.perceptor import pull_ingestion as pull_ingestion_module
from sleepagent.integrations.perceptor.reconciliation import (
    PerceptorReconciliationResult,
)
from sleepagent.integrations.perceptor.pull_ingestion import (
    DurablePerceptorPullIngress,
    PerceptorLiveNormalizationDispatcher,
    PerceptorHistoryBackfillRequired,
    PerceptorPullBackfillRunner,
    PerceptorPullIngressError,
    PerceptorPullIngressResult,
    PerceptorPullNormalizationProcessor,
    PerceptorPullQuarantineReprocessor,
    PullHistoryPlan,
    PullRequestCoordinates,
    parse_platform_success_response,
    perceptor_pull_raw_aad,
    split_history_backfill_range,
)
from sleepagent.persistence.uow import ExternalIngressScope, UowScope


UTC = timezone.utc
REQUESTED_AT = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
RECEIVED_AT = REQUESTED_AT + timedelta(seconds=1)
WINDOW_START = datetime(2026, 8, 23, 0, 45, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
CLIENT_ID_SHA256 = hashlib.sha256(b"synthetic-client-id").hexdigest()


class Cursor:
    rowcount = 1

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.statement = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, statement: str, params: tuple[Any, ...]) -> None:
        self.statement = statement
        self.params = params

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


class Uow:
    def __init__(self, cursor: Cursor) -> None:
        self.connection = self
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> Cursor:
        return self._cursor

    def __enter__(self) -> "Uow":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True


class UowFactory:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.cursor = Cursor(row)
        self.uow = Uow(self.cursor)
        self.scopes: list[object] = []

    def begin(self, scope: object) -> Uow:
        self.scopes.append(scope)
        return self.uow


class FailingUowFactory:
    def __init__(self) -> None:
        self.scopes: list[object] = []

    def begin(self, scope: object) -> Uow:
        self.scopes.append(scope)
        raise RuntimeError("synthetic database unavailable before raw commit")


def _api_scope() -> UowScope:
    return UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="api",
        purpose="device_binding_management",
        service_principal_id="api-1",
        namespace_generation=4,
        subject_id="subject-1",
        actor_id="admin-1",
        actor_role="elder",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
    )


def test_quarantine_reprocessor_uses_actor_authorized_database_boundary() -> None:
    factory = UowFactory(("work-1", "raw-1", "pending", 1))
    service = PerceptorPullQuarantineReprocessor(
        factory,  # type: ignore[arg-type]
        now_factory=lambda: RECEIVED_AT,
    )

    result = service.requeue(
        _api_scope(),
        quarantine_id="quarantine-1",
        expected_work_id="work-1",
        request_id="reprocess-1",
        actor_id="admin-1",
        authorization_id="authorization-1",
        reason="normalizer compatibility correction",
    )

    assert result.work_id == "work-1"
    assert result.raw_ingress_record_id == "raw-1"
    assert result.status == "pending"
    assert result.attempt_count == 1
    assert "sleepagent_requeue_perceptor_pull_quarantine" in factory.cursor.statement
    assert factory.cursor.params == (
        "quarantine-1",
        "work-1",
        "reprocess-1",
        "admin-1",
        "authorization-1",
        "normalizer compatibility correction",
        RECEIVED_AT,
    )
    assert factory.uow.committed is True


def test_quarantine_reprocessor_rejects_worker_self_authority() -> None:
    factory = FailingUowFactory()
    worker_scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="worker",
        service_principal_id="worker-1",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )

    with pytest.raises(ValueError, match="live API authority"):
        PerceptorPullQuarantineReprocessor(
            factory,  # type: ignore[arg-type]
            now_factory=lambda: RECEIVED_AT,
        ).requeue(
            worker_scope,
            quarantine_id="quarantine-1",
            expected_work_id="work-1",
            request_id="reprocess-1",
            actor_id="admin-1",
            authorization_id="authorization-1",
            reason="normalizer compatibility correction",
        )


def test_repeat_quarantine_evidence_is_generation_scoped() -> None:
    first = pull_ingestion_module._quarantine_evidence_ids("work-1", 1)
    second = pull_ingestion_module._quarantine_evidence_ids("work-1", 2)

    assert first == (
        pull_ingestion_module._stable_prefixed_id(
            "receipt", "work-1", "quarantine"
        ),
        pull_ingestion_module._stable_prefixed_id("quarantine", "work-1"),
    )
    assert second == pull_ingestion_module._quarantine_evidence_ids("work-1", 2)
    assert second != first


def _settings() -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="test-live-perceptor-pull",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.API,
        data_mode=ConfigDataMode.LIVE,
        database_dsn="postgresql://api:synthetic@postgres/live_db",
        database_identity="live_db",
        database_role="sleepagent_api_live",
        service_principal_id="sleepagent-perceptor-api-test",
        database_scope=ConfigDataMode.LIVE,
        namespace_prefixes=("live:perceptor-pull-test",),
        enabled_surfaces=frozenset({ApiSurface.PERCEPTOR_PUSH}),
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
        perceptor_client_secret_ref="test:perceptor-secret",
        perceptor_provider_account_id="account-1",
        perceptor_namespace_id="live:perceptor-pull-test",
        perceptor_namespace_generation=4,
        perceptor_authorization_epoch=8,
        raw_retention_seconds=3600,
    )


def _worker_settings() -> SleepBackendSettings:
    return _settings().model_copy(
        update={
            "process_role": ProcessRole.WORKER,
            "enabled_surfaces": frozenset(),
            "worker_queues": ("perceptor.history_overlap_pull",),
            "service_principal_id": "sleepagent-perceptor-worker-test",
        }
    )


def _binding(
    *,
    provider_id: str = "perceptor",
    provider_account_id: str = "account-1",
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> DeviceBinding:
    return DeviceBinding(
        data_mode=DataMode.LIVE,
        device_binding_id="binding-1",
        binding_version=2,
        device_id="internal-device-1",
        provider_id=provider_id,
        provider_account_id=provider_account_id,
        provider_device=ProviderDeviceIdentity(
            provider_device_id="provider-device-1",
            provider_device_name="SYNTHETIC-BOUND-DEVICE",
            home_id="7000000000000000001",
        ),
        subject_id="subject-1",
        timezone_name="Asia/Shanghai",
        effective_from=effective_from or REQUESTED_AT - timedelta(days=1),
        effective_until=effective_until,
        status=DeviceBindingStatus.ACTIVE,
        changed_by_actor_id="admin-1",
        change_reason="authorized synthetic fixture",
        recorded_at=REQUESTED_AT - timedelta(days=1),
    )


def _envelope(data: object, *, request_id: str = "synthetic-request-1") -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "success": True,
            "code": "200",
            "message": "synthetic",
            "data": data,
        },
        separators=(",", ":"),
    ).encode()


def _read(
    endpoint: str,
    data: object,
    *,
    raw: bytes | None = None,
    requested_at: datetime = REQUESTED_AT,
    received_at: datetime = RECEIVED_AT,
) -> PlatformEvidencedRead[Any]:
    return PlatformEvidencedRead(
        endpoint=endpoint,
        data=data,
        evidence=PlatformRawResponseEvidence(
            endpoint=endpoint,
            requested_at=requested_at,
            received_at=received_at,
            http_status=200,
            raw_body=_envelope(data) if raw is None else raw,
        ),
    )


def _sleep_report_no_data() -> dict[str, object]:
    return {
        "sleep_profile": {
            "deep_sleep_rate": None,
            "sleep_duration": None,
            "sleep_efficiency": None,
        },
        "sleep_stage_list": None,
        "heart_rate_data": None,
        "heart_rate_avg": 0,
        "breathe_data": None,
        "breathe_avg": 0,
        "body_shake_data": [],
        "sum_body_shake_times": 0,
        "getups": [],
    }


@pytest.mark.parametrize(
    "raw",
    [
        b'{"success":true,"success":true,"code":"200","data":{}}',
        b'{"success":true,"code":"200","data":{"value":NaN}}',
        b'{"success":true,"code":"200","data":{"value":1e400}}',
        b'{"success":false,"code":"200","data":{}}',
        b'{"success":true,"code":"200","data":null}',
        b'not-json',
    ],
)
def test_exact_success_parser_rejects_ambiguous_or_non_success_json(raw: bytes) -> None:
    with pytest.raises(PerceptorPullIngressError):
        parse_platform_success_response(raw)


def test_exact_success_parser_returns_only_exact_envelope_data() -> None:
    data = {"nested": [{"value": 1}], "empty": []}

    assert parse_platform_success_response(_envelope(data)) == data


@pytest.mark.parametrize(
    ("start_at", "end_at", "message"),
    [
        (WINDOW_START, WINDOW_START, "positive"),
        (WINDOW_START, WINDOW_START + timedelta(hours=1, seconds=1), "one hour"),
        (
            WINDOW_START.replace(microsecond=1),
            WINDOW_END,
            "whole-second",
        ),
        (WINDOW_START.replace(tzinfo=None), WINDOW_END, "timezone-aware"),
    ],
)
def test_history_coordinates_fail_closed_outside_exact_vendor_bound(
    start_at: datetime,
    end_at: datetime,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=start_at,
            window_end_at=end_at,
        )


def test_pull_raw_aad_is_exact_scope_and_authenticates_ciphertext() -> None:
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    raw = _envelope({"HeartRate": 60})
    aad = perceptor_pull_raw_aad("live:tenant", 3, "raw-1")
    encrypted = cipher.encrypt(raw, aad=aad)

    assert cipher.decrypt(encrypted, aad=aad) == raw
    with pytest.raises(SleepSliceInvariantError):
        cipher.decrypt(
            encrypted,
            aad=perceptor_pull_raw_aad("live:tenant", 4, "raw-1"),
        )
    with pytest.raises(ValueError, match="exact durable scope"):
        perceptor_pull_raw_aad("live:tenant\0other", 3, "raw-1")


def test_history_ingress_encrypts_exact_response_and_persists_three_second_overlap() -> None:
    binding = _binding()
    history = (
        {
            "device_id": "provider-device-1",
            "heart_rate": [60],
            "body_shake": [0],
            "breath_rate": [15],
            "send_time": int(WINDOW_END.timestamp()),
        },
    )
    raw = _envelope(list(history))
    factory = UowFactory(
        ("accepted", "raw-db-1", "work-db-1", "subject-1", "binding-1", False)
    )
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=cipher,
    )

    result = service.accept(
        _read(HISTORY_ENDPOINT, history, raw=raw),
        binding=binding,
        coordinates=PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=WINDOW_START,
            window_end_at=WINDOW_END,
        ),
    )

    assert result.disposition == "accepted"
    assert factory.uow.committed is True
    assert factory.scopes == [
        ExternalIngressScope(
            namespace_id="live:perceptor-pull-test",
            namespace_generation=4,
            service_principal_id="sleepagent-perceptor-api-test",
            authorization_epoch=8,
        )
    ]
    params = factory.cursor.params
    assert len(params) == 30
    assert params[8] == HISTORY_ENDPOINT
    assert params[9:12] == (WINDOW_START, WINDOW_END, None)
    assert params[16] == hashlib.sha256(raw).hexdigest()
    assert params[17] != raw
    assert params[20] == len(raw)
    assert params[22:24] == (True, True)
    assert params[6] == "provider-device-1"
    metadata = json.loads(params[29])
    cursor_at = datetime.fromisoformat(metadata["checkpoint_cursor_at"])
    watermark = datetime.fromisoformat(metadata["lateness_watermark_at"])
    assert cursor_at == WINDOW_END
    assert cursor_at - watermark == timedelta(seconds=3)
    assert metadata["source_channel"] == "PULL"
    assert metadata["provider_device_key"].startswith("sha256:")
    assert cipher.decrypt(
        params[17],
        aad=perceptor_pull_raw_aad(
            "live:perceptor-pull-test",
            4,
            params[24],
        ),
    ) == raw


@pytest.mark.parametrize("resumed", [False, True])
def test_history_planner_consumes_exact_scoped_checkpoint_watermark(
    resumed: bool,
) -> None:
    cursor_at = WINDOW_START + timedelta(minutes=5) if resumed else None
    watermark = cursor_at - timedelta(seconds=3) if cursor_at is not None else None
    planned_start = watermark if watermark is not None else WINDOW_START
    factory = UowFactory(
        (planned_start, WINDOW_END, cursor_at, watermark, resumed)
    )
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    plan = service.plan_history_window(
        binding=_binding(),
        requested_start_at=WINDOW_START,
        requested_end_at=WINDOW_END,
    )

    assert plan.window_start_at == planned_start
    assert plan.window_end_at == WINDOW_END
    assert plan.resumed_from_checkpoint is resumed
    assert "sleepagent_plan_perceptor_history" in factory.cursor.statement
    assert factory.cursor.params == (
        "live:perceptor-pull-test",
        4,
        "account-1",
        "binding-1",
        2,
        "subject-1",
        WINDOW_START,
        WINDOW_END,
    )
    assert factory.scopes == [
        ExternalIngressScope(
            namespace_id="live:perceptor-pull-test",
            namespace_generation=4,
            service_principal_id="sleepagent-perceptor-api-test",
            authorization_epoch=8,
        )
    ]


def test_scheduled_history_reuses_exact_claimed_worker_scope() -> None:
    factory = UowFactory((WINDOW_START, WINDOW_END, None, None, False))
    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-perceptor-worker-test",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=8,
        privacy_epoch=9,
        retrieval_policy_epoch=10,
        worker_instance="scheduled-worker-1",
    )
    service = DurablePerceptorPullIngress(
        _worker_settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
        operation_scope=scope,
    )

    service.plan_history_window(
        binding=_binding(),
        requested_start_at=WINDOW_START,
        requested_end_at=WINDOW_END,
    )

    assert factory.scopes == [scope]


def test_scheduled_pull_rejects_scope_outside_bound_subject() -> None:
    factory = UowFactory((WINDOW_START, WINDOW_END, None, None, False))
    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-perceptor-worker-test",
        namespace_generation=4,
        subject_id="other-subject",
        authorization_epoch=8,
        privacy_epoch=9,
        retrieval_policy_epoch=10,
        worker_instance="scheduled-worker-1",
    )
    service = DurablePerceptorPullIngress(
        _worker_settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
        operation_scope=scope,
    )

    with pytest.raises(PerceptorPullIngressError, match="binding authority"):
        service.plan_history_window(
            binding=_binding(),
            requested_start_at=WINDOW_START,
            requested_end_at=WINDOW_END,
        )

    assert factory.scopes == []


def test_history_planner_fails_closed_on_non_exact_checkpoint_watermark() -> None:
    cursor_at = WINDOW_START + timedelta(minutes=5)
    factory = UowFactory(
        (
            cursor_at - timedelta(seconds=2),
            WINDOW_END,
            cursor_at,
            cursor_at - timedelta(seconds=2),
            True,
        )
    )
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    with pytest.raises(ValueError, match="exact three-second watermark"):
        service.plan_history_window(
            binding=_binding(),
            requested_start_at=WINDOW_START,
            requested_end_at=WINDOW_END,
        )


def test_history_pre_binding_period_is_rejected_before_raw_uow() -> None:
    factory = UowFactory()
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    with pytest.raises(ValueError, match="precedes DeviceBinding effective interval"):
        service.accept(
            _read(HISTORY_ENDPOINT, [{"device_id": "provider-device-1"}]),
            binding=_binding(effective_from=WINDOW_START + timedelta(seconds=1)),
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
            ),
        )

    assert factory.scopes == []


def test_history_window_crossing_reassignment_is_rejected_before_raw_uow() -> None:
    factory = UowFactory()
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    reassigned_at = WINDOW_START + timedelta(minutes=5)
    requested_at = WINDOW_START + timedelta(minutes=1)

    with pytest.raises(ValueError, match="exceeds DeviceBinding effective interval"):
        service.accept(
            _read(
                HISTORY_ENDPOINT,
                [{"device_id": "provider-device-1"}],
                requested_at=requested_at,
                received_at=requested_at + timedelta(seconds=1),
            ),
            binding=_binding(effective_until=reassigned_at),
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
            ),
        )

    assert factory.scopes == []


@pytest.mark.parametrize(
    ("effective_from", "effective_until", "message"),
    [
        (
            datetime(2026, 8, 21, 16, 0, 1, tzinfo=UTC),
            None,
            "precedes DeviceBinding effective interval",
        ),
        (
            datetime(2026, 8, 21, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 22, 15, 59, 59, tzinfo=UTC),
            "exceeds DeviceBinding effective interval",
        ),
    ],
)
def test_sleep_report_requires_full_binding_local_calendar_day_before_raw_uow(
    effective_from: datetime,
    effective_until: datetime | None,
    message: str,
) -> None:
    factory = UowFactory()
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    with pytest.raises(ValueError, match=message):
        service.accept(
            _read(SLEEP_REPORT_ENDPOINT, _sleep_report_no_data()),
            binding=_binding(
                effective_from=effective_from,
                effective_until=effective_until,
            ),
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 22),
            ),
        )

    assert factory.scopes == []


def test_database_failure_before_raw_commit_never_returns_durable_acceptance() -> None:
    factory = FailingUowFactory()
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    history = ({
        "device_id": "provider-device-1",
        "heart_rate": "60",
        "breath_rate": "14",
        "body_shake": "0",
        "send_time": "2026-08-23T09:00:00",
    },)

    with pytest.raises(RuntimeError, match="before raw commit"):
        service.accept(
            _read(HISTORY_ENDPOINT, history),
            binding=_binding(),
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
            ),
        )

    assert len(factory.scopes) == 1


def test_history_response_name_is_labeled_as_name_not_provider_id() -> None:
    history = ({"device_id": "SYNTHETIC-BOUND-DEVICE"},)
    factory = UowFactory(
        ("accepted", "raw-db-1", "work-db-1", "subject-1", "binding-1", False)
    )
    DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    ).accept(
        _read(HISTORY_ENDPOINT, history, raw=_envelope(list(history))),
        binding=_binding(),
        coordinates=PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=WINDOW_START,
            window_end_at=WINDOW_END,
        ),
    )

    assert factory.cursor.params[6] is None
    assert factory.cursor.params[7] == "SYNTHETIC-BOUND-DEVICE"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ([{"device_id": "provider-device-1"}], ("provider-device-1", None)),
        ([{"device_id": "SYNTHETIC-BOUND-DEVICE"}], (None, "SYNTHETIC-BOUND-DEVICE")),
        ([{"device_id": "unknown"}], ("__response_device_mismatch__", None)),
        (
            [
                {"device_id": "provider-device-1"},
                {"device_id": "SYNTHETIC-BOUND-DEVICE"},
            ],
            ("__response_device_mismatch__", None),
        ),
        ([{"heart_rate": [60]}], ("__response_device_mismatch__", None)),
    ],
)
def test_history_response_identity_is_exactly_classified_for_binding(
    data: list[dict[str, object]],
    expected: tuple[str | None, str | None],
) -> None:
    assert pull_ingestion_module._response_device_identity(
        HISTORY_ENDPOINT,
        data,
        _binding(),
    ) == expected


def test_same_semantic_history_request_has_stable_batch_despite_envelope_and_offset() -> None:
    binding = _binding()
    history = ({"device_id": "provider-device-1"},)
    first_factory = UowFactory(
        ("accepted", "raw-1", "work-1", "subject-1", "binding-1", False)
    )
    second_factory = UowFactory(
        ("accepted", "raw-2", "work-1", "subject-1", "binding-1", False)
    )
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    coordinates_utc = PullRequestCoordinates(
        endpoint=HISTORY_ENDPOINT,
        window_start_at=WINDOW_START,
        window_end_at=WINDOW_END,
    )
    offset = timezone(timedelta(hours=8))
    coordinates_offset = PullRequestCoordinates(
        endpoint=HISTORY_ENDPOINT,
        window_start_at=WINDOW_START.astimezone(offset),
        window_end_at=WINDOW_END.astimezone(offset),
    )
    first = DurablePerceptorPullIngress(
        _settings(), first_factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256, cipher=cipher,
    ).accept(
        _read(HISTORY_ENDPOINT, history, raw=_envelope(list(history), request_id="one")),
        binding=binding,
        coordinates=coordinates_utc,
    )
    second = DurablePerceptorPullIngress(
        _settings(), second_factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256, cipher=cipher,
    ).accept(
        _read(HISTORY_ENDPOINT, history, raw=_envelope(list(history), request_id="two")),
        binding=binding,
        coordinates=coordinates_offset,
    )

    assert first.batch_identity == second.batch_identity
    assert first.response_semantic_sha256 == second.response_semantic_sha256
    assert first_factory.cursor.params[24] != second_factory.cursor.params[24]
    assert first_factory.cursor.params[25] == second_factory.cursor.params[25]


def test_ingress_fails_closed_if_database_resolves_another_binding() -> None:
    factory = UowFactory(
        ("accepted", "raw-1", "work-1", "subject-1", "binding-other", False)
    )
    service = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    history = ({"device_id": "provider-device-1"},)

    with pytest.raises(PerceptorPullIngressError, match="DeviceBinding"):
        service.accept(
            _read(HISTORY_ENDPOINT, history, raw=_envelope(list(history))),
            binding=_binding(),
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
            ),
        )

    assert factory.uow.committed is False


@pytest.mark.parametrize(
    ("provider_id", "account", "message"),
    [
        ("other", "account-1", "Perceptor binding"),
        ("perceptor", "account-other", "provider account"),
    ],
)
def test_ingress_rejects_binding_outside_configured_authority(
    provider_id: str,
    account: str,
    message: str,
) -> None:
    service = DurablePerceptorPullIngress(
        _settings(),
        UowFactory(),  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    with pytest.raises(ValueError, match=message):
        service.accept(
            _read(REALTIME_READ_ENDPOINT, {}),
            binding=_binding(provider_id=provider_id, provider_account_id=account),
            coordinates=PullRequestCoordinates(endpoint=REALTIME_READ_ENDPOINT),
        )


def test_sleep_report_no_data_is_durable_but_has_no_normalized_candidates() -> None:
    data = _sleep_report_no_data()
    raw = _envelope(data)
    factory = UowFactory(
        ("accepted", "raw-db-1", "work-db-1", "subject-1", "binding-1", False)
    )
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    service = DurablePerceptorPullIngress(
        _settings(), factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256, cipher=cipher,
    )
    service.accept(
        _read(SLEEP_REPORT_ENDPOINT, data, raw=raw),
        binding=_binding(effective_from=REQUESTED_AT - timedelta(days=2)),
        coordinates=PullRequestCoordinates(
            endpoint=SLEEP_REPORT_ENDPOINT,
            report_date=date(2026, 8, 22),
        ),
    )
    params = factory.cursor.params
    assert params[22:24] == (True, False)

    raw_id = str(params[24])
    normalized = PerceptorPullNormalizationProcessor(
        UowFactory(),  # type: ignore[arg-type]
        cipher=cipher,
    )._normalize_loaded(
        {
            "namespace_id": "live:perceptor-pull-test",
            "namespace_generation": 4,
            "raw_ingress_record_id": raw_id,
            "encrypted_payload": params[17],
            "payload_sha256": params[16],
            "provider_account_id": "account-1",
            "requested_at": REQUESTED_AT,
            "received_at": RECEIVED_AT,
            "work_json": {
                "endpoint": SLEEP_REPORT_ENDPOINT,
                "requested_report_date": "2026-08-22",
                "response_has_data": False,
                "response_semantic_sha256": hashlib.sha256(
                    json.dumps(
                        data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            },
            "binding_json": _binding(
                effective_from=REQUESTED_AT - timedelta(days=2)
            ).model_dump(mode="json"),
        }
    )

    assert normalized["no_data"] is True
    assert normalized["candidates"] == ()
    assert normalized["observations"] == ()


def test_sleep_report_worker_binds_summary_to_authoritative_report_window() -> None:
    data = {
        "sleep_profile": {"sleep_time": "22-06"},
        "sleep_stage_list": [
            {
                "start_time": 1787385600,
                "end_time": 1787414400,
                "type": 2,
            }
        ],
        "heart_rate_avg": 64,
    }
    raw = _envelope(data)
    raw_id = "perceptor:pull:raw:report-period-anchor"
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    encrypted = cipher.encrypt(
        raw,
        aad=perceptor_pull_raw_aad(
            "live:perceptor-pull-test",
            4,
            raw_id,
        ),
    )

    normalized = PerceptorPullNormalizationProcessor(
        UowFactory(),  # type: ignore[arg-type]
        cipher=cipher,
    )._normalize_loaded(
        {
            "namespace_id": "live:perceptor-pull-test",
            "namespace_generation": 4,
            "raw_ingress_record_id": raw_id,
            "encrypted_payload": encrypted,
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "provider_account_id": "account-1",
            "requested_at": REQUESTED_AT,
            "received_at": RECEIVED_AT,
            "work_json": {
                "endpoint": SLEEP_REPORT_ENDPOINT,
                "requested_report_date": "2026-08-22",
                "response_has_data": True,
                "response_semantic_sha256": hashlib.sha256(
                    json.dumps(
                        data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            },
            "binding_json": _binding(
                effective_from=REQUESTED_AT - timedelta(days=2)
            ).model_dump(mode="json"),
        }
    )

    assert len(normalized["observations"]) == 2
    summary = next(
        observation
        for observation in normalized["observations"]
        if observation.observation_type
        is ObservationType.VENDOR_SLEEP_PROFILE_METRIC
    )
    assert summary.measurement_at == datetime.fromtimestamp(1787414400, tz=UTC)
    assert summary.source_timestamp_text == "2026-08-22"
    assert normalized["intentionally_unsupported_fields"] == (
        "sleep_profile.sleep_time",
    )


def test_history_normalization_rejects_reconstructed_sample_after_request_window() -> None:
    data = [
        {
            "device_id": "provider-device-1",
            "heart_rate": "60",
            "breath_rate": "15",
            "body_shake": "0",
            "send_time": "2026-08-23 09:00:03",
        }
    ]
    raw = _envelope(data)
    raw_id = "perceptor:pull:raw:outside-window"
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    encrypted = cipher.encrypt(
        raw,
        aad=perceptor_pull_raw_aad(
            "live:perceptor-pull-test",
            4,
            raw_id,
        ),
    )

    with pytest.raises(PullContractError, match="after the exact requested window"):
        PerceptorPullNormalizationProcessor(
            UowFactory(),  # type: ignore[arg-type]
            cipher=cipher,
        )._normalize_loaded(
            {
                "namespace_id": "live:perceptor-pull-test",
                "namespace_generation": 4,
                "raw_ingress_record_id": raw_id,
                "encrypted_payload": encrypted,
                "payload_sha256": hashlib.sha256(raw).hexdigest(),
                "provider_account_id": "account-1",
                "requested_at": REQUESTED_AT,
                "received_at": RECEIVED_AT,
                "work_json": {
                    "endpoint": HISTORY_ENDPOINT,
                    "response_has_data": True,
                    "requested_window_start": WINDOW_START.isoformat(),
                    "requested_window_end": WINDOW_END.isoformat(),
                    "response_semantic_sha256": hashlib.sha256(
                        json.dumps(
                            data,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                },
                "binding_json": _binding().model_dump(mode="json"),
            }
        )


@pytest.mark.parametrize(
    ("persisted_has_data", "message"),
    [
        (True, "contradicts normalization"),
        ("false", "strict boolean"),
        (None, "strict boolean"),
    ],
)
def test_normalization_rejects_persisted_has_data_contradiction(
    persisted_has_data: object,
    message: str,
) -> None:
    data: dict[str, object] = {}
    raw = _envelope(data)
    raw_id = "perceptor:pull:raw:has-data-mismatch"
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    encrypted = cipher.encrypt(
        raw,
        aad=perceptor_pull_raw_aad(
            "live:perceptor-pull-test",
            4,
            raw_id,
        ),
    )

    with pytest.raises(SleepSliceInvariantError, match=message):
        PerceptorPullNormalizationProcessor(
            UowFactory(),  # type: ignore[arg-type]
            cipher=cipher,
        )._normalize_loaded(
            {
                "namespace_id": "live:perceptor-pull-test",
                "namespace_generation": 4,
                "raw_ingress_record_id": raw_id,
                "encrypted_payload": encrypted,
                "payload_sha256": hashlib.sha256(raw).hexdigest(),
                "provider_account_id": "account-1",
                "requested_at": REQUESTED_AT,
                "received_at": RECEIVED_AT,
                "work_json": {
                    "endpoint": GET_CURRENT_ENDPOINT,
                    "response_has_data": persisted_has_data,
                    "response_semantic_sha256": hashlib.sha256(b"{}").hexdigest(),
                },
                "binding_json": _binding().model_dump(mode="json"),
            }
        )


def test_checkpoint_links_canonical_evidence_and_allows_null_only_for_no_data() -> None:
    class SequenceCursor(Cursor):
        def __init__(self) -> None:
            super().__init__(("present",))
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            self.statement = statement
            self.params = params
            self.calls.append((statement, params))

    class SequenceFactory(UowFactory):
        def __init__(self) -> None:
            self.cursor = SequenceCursor()
            self.uow = Uow(self.cursor)
            self.scopes = []

    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="normalization",
        service_principal_id="worker-1",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-instance-1",
    )
    lease = NormalizationLease(
        work_id="work-1",
        lease_generation=2,
        fencing_token="f" * 32,
        worker_instance="worker-instance-1",
    )
    loaded = {
        "raw_ingress_record_id": "raw-1",
        "work_json": {
            "checkpoint_cursor_at": WINDOW_END.isoformat(),
            "lateness_watermark_at": (
                WINDOW_END - timedelta(seconds=3)
            ).isoformat(),
            "stream_key": "stream-1",
            "data_surface": "history",
            "device_binding_id": "binding-1",
            "binding_version": 2,
            "overlap_seconds": 3,
            "provider_account_id": "account-1",
        },
    }
    factory = SequenceFactory()
    processor = PerceptorPullNormalizationProcessor(
        factory,  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    assert processor._advance_checkpoint_and_finalize(
        scope,
        lease,
        loaded=loaded,
        reconciliation_summary={
            "canonical_observation_ids": ["observation-z", "observation-a"],
            "no_data": False,
        },
        committed_at=RECEIVED_AT,
    )
    checkpoint_call = next(
        call
        for call in factory.cursor.calls
        if "INSERT INTO public.sleep_domain_pull_checkpoints" in call[0]
    )
    assert "last_canonical_observation_id" in checkpoint_call[0]
    assert checkpoint_call[1][-2:] == ("observation-z", RECEIVED_AT)

    with pytest.raises(SleepSliceInvariantError, match="canonical evidence"):
        processor._advance_checkpoint_and_finalize(
            scope,
            lease,
            loaded=loaded,
            reconciliation_summary={
                "canonical_observation_ids": [],
                "no_data": False,
            },
            committed_at=RECEIVED_AT,
        )

    no_data_factory = SequenceFactory()
    no_data_processor = PerceptorPullNormalizationProcessor(
        no_data_factory,  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    assert no_data_processor._advance_checkpoint_and_finalize(
        scope,
        lease,
        loaded=loaded,
        reconciliation_summary={
            "canonical_observation_ids": [],
            "no_data": True,
        },
        committed_at=RECEIVED_AT,
    )
    no_data_checkpoint_call = next(
        call
        for call in no_data_factory.cursor.calls
        if "INSERT INTO public.sleep_domain_pull_checkpoints" in call[0]
    )
    assert no_data_checkpoint_call[1][-2:] == (None, RECEIVED_AT)

    for contradictory_summary in (
        {"canonical_observation_ids": [], "no_data": "true"},
        {"canonical_observation_ids": ["observation-a"], "no_data": True},
    ):
        with pytest.raises(SleepSliceInvariantError, match="checkpoint"):
            processor._advance_checkpoint_and_finalize(
                scope,
                lease,
                loaded=loaded,
                reconciliation_summary=contradictory_summary,
                committed_at=RECEIVED_AT,
            )


def test_stream_key_changes_across_namespace_generation() -> None:
    binding = _binding()
    data = ({"device_id": "provider-device-1"},)
    raw = _envelope(list(data))

    def accepted_identities(generation: int) -> tuple[str, str, str]:
        settings = _settings().model_copy(
            update={"perceptor_namespace_generation": generation}
        )
        factory = UowFactory(
            ("accepted", "raw-1", "work-1", "subject-1", "binding-1", False)
        )
        DurablePerceptorPullIngress(
            settings,
            factory,  # type: ignore[arg-type]
            client_id_sha256=CLIENT_ID_SHA256,
            cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
        ).accept(
            _read(HISTORY_ENDPOINT, data, raw=raw),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
            ),
        )
        return (
            str(json.loads(factory.cursor.params[29])["stream_key"]),
            str(factory.cursor.params[24]),
            str(factory.cursor.params[25]),
        )

    first = accepted_identities(4)
    second = accepted_identities(5)
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[2] != second[2]


def test_backfill_runner_uses_exactly_one_bound_device_and_refuses_large_window() -> None:
    history = ({"device_id": "provider-device-1"},)
    read = _read(HISTORY_ENDPOINT, history, raw=_envelope(list(history)))

    class Client:
        calls: list[tuple[tuple[str, ...], datetime, datetime]] = []

        def get_history_evidenced(
            self,
            *,
            device_names: tuple[str, ...],
            start_at: datetime,
            end_at: datetime,
        ) -> PlatformEvidencedRead[Any]:
            self.calls.append((device_names, start_at, end_at))
            return read

    class Ingress:
        calls: list[tuple[DeviceBinding, PullRequestCoordinates]] = []
        plan_calls: list[tuple[DeviceBinding, datetime, datetime]] = []
        next_plan: PullHistoryPlan | None = None
        settings = _settings()

        def plan_history_window(
            self,
            *,
            binding: DeviceBinding,
            requested_start_at: datetime,
            requested_end_at: datetime,
        ) -> PullHistoryPlan:
            self.plan_calls.append(
                (binding, requested_start_at, requested_end_at)
            )
            if self.next_plan is not None:
                return self.next_plan
            return PullHistoryPlan(
                window_start_at=requested_start_at,
                window_end_at=requested_end_at,
                checkpoint_cursor_at=None,
                lateness_watermark_at=None,
                resumed_from_checkpoint=False,
            )

        def accept(
            self,
            _read_value: PlatformEvidencedRead[Any],
            *,
            binding: DeviceBinding,
            coordinates: PullRequestCoordinates,
        ) -> PerceptorPullIngressResult:
            self.calls.append((binding, coordinates))
            return PerceptorPullIngressResult(
                disposition="accepted",
                raw_ingress_record_id="raw-1",
                normalization_work_id="work-1",
                subject_id="subject-1",
                device_binding_id="binding-1",
                duplicate=False,
                batch_identity="batch-1",
                response_semantic_sha256="a" * 64,
            )

    client = Client()
    ingress = Ingress()
    runner = PerceptorPullBackfillRunner(
        client,  # type: ignore[arg-type]
        ingress,  # type: ignore[arg-type]
        _binding(),
    )

    assert (
        runner.pull_history(start_at=WINDOW_START, end_at=WINDOW_END).disposition
        == "accepted"
    )
    assert client.calls == [
        (("SYNTHETIC-BOUND-DEVICE",), WINDOW_START, WINDOW_END)
    ]
    assert ingress.calls[0][1].endpoint == HISTORY_ENDPOINT
    resume_cursor = WINDOW_END
    resumed_end = WINDOW_END + timedelta(minutes=5)
    ingress.next_plan = PullHistoryPlan(
        window_start_at=resume_cursor - timedelta(seconds=3),
        window_end_at=resumed_end,
        checkpoint_cursor_at=resume_cursor,
        lateness_watermark_at=resume_cursor - timedelta(seconds=3),
        resumed_from_checkpoint=True,
    )
    assert (
        runner.pull_history(
            start_at=WINDOW_END + timedelta(minutes=4),
            end_at=resumed_end,
        ).disposition
        == "accepted"
    )
    assert client.calls[-1] == (
        ("SYNTHETIC-BOUND-DEVICE",),
        resume_cursor - timedelta(seconds=3),
        resumed_end,
    )
    assert ingress.calls[-1][1].window_start_at == (
        resume_cursor - timedelta(seconds=3)
    )
    calls_before_checkpoint_noop = len(client.calls)
    ingress_calls_before_checkpoint_noop = len(ingress.calls)
    ingress.next_plan = PullHistoryPlan(
        window_start_at=resume_cursor - timedelta(seconds=3),
        window_end_at=resume_cursor,
        checkpoint_cursor_at=resume_cursor,
        lateness_watermark_at=resume_cursor - timedelta(seconds=3),
        resumed_from_checkpoint=True,
    )
    checkpoint_noop = runner.pull_history(
        start_at=WINDOW_START,
        end_at=resume_cursor,
    )
    assert checkpoint_noop.disposition == "checkpoint_already_advanced"
    assert checkpoint_noop.duplicate is True
    assert checkpoint_noop.raw_ingress_record_id is None
    assert checkpoint_noop.normalization_work_id is None
    assert checkpoint_noop.response_semantic_sha256 is None
    assert len(client.calls) == calls_before_checkpoint_noop
    assert len(ingress.calls) == ingress_calls_before_checkpoint_noop
    ingress.next_plan = None
    with pytest.raises(ValueError, match="one hour"):
        runner.pull_history(
            start_at=WINDOW_START,
            end_at=WINDOW_START + timedelta(hours=1, seconds=1),
        )
    assert len(client.calls) == 2


def test_live_dispatcher_routes_existing_queue_by_durable_normalizer() -> None:
    class Processor:
        def __init__(self, value: str) -> None:
            self.value = value
            self.calls: list[tuple[UowScope, NormalizationLease]] = []

        def process(self, scope: UowScope, lease: NormalizationLease) -> str:
            self.calls.append((scope, lease))
            return self.value

    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="normalization",
        service_principal_id="worker-1",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-instance-1",
    )
    lease = NormalizationLease(
        work_id="work-1",
        lease_generation=2,
        fencing_token="f" * 32,
        worker_instance="worker-instance-1",
    )
    dispatcher = PerceptorLiveNormalizationDispatcher(
        UowFactory(),  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    push = Processor("push")
    pull = Processor("pull")
    dispatcher._push = push
    dispatcher._pull = pull  # type: ignore[assignment]

    dispatcher._normalizer = lambda _scope, _lease: "perceptor_push"  # type: ignore[method-assign]
    assert dispatcher.process(scope, lease) == "push"
    dispatcher._normalizer = lambda _scope, _lease: "perceptor_pull"  # type: ignore[method-assign]
    assert dispatcher.process(scope, lease) == "pull"
    dispatcher._normalizer = lambda _scope, _lease: "other"  # type: ignore[method-assign]
    with pytest.raises(SleepSliceInvariantError, match="unsupported"):
        dispatcher.process(scope, lease)

    assert push.calls == [(scope, lease)]
    assert pull.calls == [(scope, lease)]


def test_explicit_history_backfill_chunks_are_deterministic_and_whole_second() -> None:
    start = WINDOW_START.replace(microsecond=987654)
    end = start + timedelta(hours=12, minutes=7, microseconds=100)

    chunks = split_history_backfill_range(start_at=start, end_at=end)

    assert len(chunks) == 13
    assert chunks[0][0] == WINDOW_START
    assert chunks[-1][1] == end.replace(microsecond=0)
    assert chunks[-1][1] - chunks[-1][0] == timedelta(minutes=7)
    assert all(
        left.microsecond == right.microsecond == 0
        and timedelta(0) < right - left <= timedelta(hours=1)
        for left, right in chunks
    )
    assert chunks == split_history_backfill_range(start_at=start, end_at=end)


def test_explicit_history_backfill_repeats_stable_chunks_without_planner() -> None:
    records = (
        {
            "device_id": "provider-device-1",
            "heart_rate": "1,2",
            "breath_rate": "1,2",
            "body_shake": "1,2",
        },
    )

    class Client:
        calls: list[tuple[datetime, datetime]] = []

        def get_history_evidenced(
            self,
            *,
            device_names: tuple[str, ...],
            start_at: datetime,
            end_at: datetime,
        ) -> PlatformEvidencedRead[Any]:
            assert device_names == ("SYNTHETIC-BOUND-DEVICE",)
            self.calls.append((start_at, end_at))
            return _read(HISTORY_ENDPOINT, records, raw=_envelope(list(records)))

    class Ingress:
        calls: list[PullRequestCoordinates] = []

        def accept(
            self,
            _value: PlatformEvidencedRead[Any],
            *,
            binding: DeviceBinding,
            coordinates: PullRequestCoordinates,
        ) -> PerceptorPullIngressResult:
            assert binding == _binding()
            self.calls.append(coordinates)
            duplicate = len(self.calls) > 2
            return PerceptorPullIngressResult(
                disposition="duplicate" if duplicate else "accepted",
                raw_ingress_record_id="raw-1",
                normalization_work_id="work-1",
                subject_id=binding.subject_id,
                device_binding_id=binding.device_binding_id,
                duplicate=duplicate,
                batch_identity="batch-1",
                response_semantic_sha256="a" * 64,
            )

    client = Client()
    ingress = Ingress()
    runner = PerceptorPullBackfillRunner(
        client,  # type: ignore[arg-type]
        ingress,  # type: ignore[arg-type]
        _binding(),
    )
    end = WINDOW_START + timedelta(hours=1, minutes=5)

    first = runner.backfill_history(start_at=WINDOW_START, end_at=end)
    second = runner.backfill_history(start_at=WINDOW_START, end_at=end)

    assert len(first) == len(second) == 2
    assert [item.status for item in first] == ["succeeded", "succeeded"]
    assert [item.raw_series_sample_count for item in first] == [6, 6]
    assert all(item.historical_backfill for item in ingress.calls)
    assert ingress.calls[:2] == ingress.calls[2:]
    assert client.calls[:2] == client.calls[2:]
    assert all(item.ingress is not None and item.ingress.duplicate for item in second)


def test_backfill_normalization_finalizes_without_advancing_any_checkpoint() -> None:
    class SequenceCursor(Cursor):
        def __init__(self) -> None:
            super().__init__(("present",))
            self.calls: list[str] = []

        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            super().execute(statement, params)
            self.calls.append(statement)

    factory = UowFactory()
    factory.cursor = SequenceCursor()
    factory.uow = Uow(factory.cursor)
    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="normalization",
        service_principal_id="worker-1",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-instance-1",
    )
    lease = NormalizationLease(
        work_id="work-1",
        lease_generation=2,
        fencing_token="f" * 32,
        worker_instance="worker-instance-1",
    )
    binding = _binding()
    backfill_stream = pull_ingestion_module._stream_key(
        binding,
        HISTORY_ENDPOINT,
        namespace_generation=4,
        historical_backfill=True,
    )
    loaded = {
        "raw_ingress_record_id": "raw-1",
        "binding_json": binding.model_dump(mode="json"),
        "work_json": {
            "endpoint": HISTORY_ENDPOINT,
            "checkpoint_cursor_at": WINDOW_END.isoformat(),
            "lateness_watermark_at": (
                WINDOW_END - timedelta(seconds=3)
            ).isoformat(),
            "stream_key": backfill_stream,
            "data_surface": "history",
            "device_binding_id": "binding-1",
            "binding_version": 2,
            "overlap_seconds": 3,
            "provider_account_id": "account-1",
        },
    }
    processor = PerceptorPullNormalizationProcessor(
        factory,  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    advanced = processor._advance_checkpoint_and_finalize(
        scope,
        lease,
        loaded=loaded,
        reconciliation_summary={
            "canonical_observation_ids": ["observation-1"],
            "no_data": False,
        },
        committed_at=RECEIVED_AT,
    )

    assert advanced is False
    assert not any(
        "INSERT INTO public.sleep_domain_pull_checkpoints" in statement
        for statement in factory.cursor.calls
    )
    assert any(
        "SET status = 'succeeded'" in statement
        for statement in factory.cursor.calls
    )


def test_stale_history_checkpoint_is_classified_for_explicit_backfill() -> None:
    class Diagnostic:
        message_primary = "Perceptor history continuation exceeds one hour"

    class PlannerError(RuntimeError):
        diag = Diagnostic()

    class FailingCursor(Cursor):
        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            raise PlannerError("redacted")

    factory = UowFactory()
    factory.cursor = FailingCursor(None)
    factory.uow = Uow(factory.cursor)
    ingress = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    with pytest.raises(PerceptorHistoryBackfillRequired):
        ingress.plan_history_window(
            binding=_binding(),
            requested_start_at=WINDOW_START,
            requested_end_at=WINDOW_END,
        )


def test_exact_date_sleep_report_recovery_retry_never_drifts() -> None:
    report_date = date(2026, 9, 5)
    read = _read(SLEEP_REPORT_ENDPOINT, _sleep_report_no_data())

    class Client:
        dates: list[date] = []

        def get_sleep_report_evidenced(
            self, *, device_name: str, home_id: str, report_date: date
        ) -> PlatformEvidencedRead[Any]:
            assert device_name == "SYNTHETIC-BOUND-DEVICE"
            assert home_id == "7000000000000000001"
            self.dates.append(report_date)
            return read

    class Ingress:
        coordinates: list[PullRequestCoordinates] = []

        def accept(
            self,
            _value: PlatformEvidencedRead[Any],
            *,
            binding: DeviceBinding,
            coordinates: PullRequestCoordinates,
        ) -> PerceptorPullIngressResult:
            self.coordinates.append(coordinates)
            return PerceptorPullIngressResult(
                disposition="accepted",
                raw_ingress_record_id="raw-report",
                normalization_work_id="work-report",
                subject_id=binding.subject_id,
                device_binding_id=binding.device_binding_id,
                duplicate=False,
                batch_identity="batch-report",
                response_semantic_sha256="b" * 64,
            )

    client = Client()
    ingress = Ingress()
    runner = PerceptorPullBackfillRunner(
        client,  # type: ignore[arg-type]
        ingress,  # type: ignore[arg-type]
        _binding(),
    )

    runner.pull_sleep_report(report_date)
    runner.pull_sleep_report(report_date)

    assert client.dates == [report_date, report_date]
    assert [item.report_date for item in ingress.coordinates] == [
        report_date,
        report_date,
    ]


def test_recovery_ingress_atomically_defers_new_normalization_work() -> None:
    class RecordingCursor(Cursor):
        def __init__(self) -> None:
            super().__init__(
                ("accepted", "raw-1", "work-1", "subject-1", "binding-1", False)
            )
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            super().execute(statement, params)
            self.calls.append((statement, params))

    factory = UowFactory()
    factory.cursor = RecordingCursor()
    factory.uow = Uow(factory.cursor)
    data = ({"device_id": "provider-device-1"},)
    ingress = DurablePerceptorPullIngress(
        _settings(),
        factory,  # type: ignore[arg-type]
        client_id_sha256=CLIENT_ID_SHA256,
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )

    result = ingress.accept(
        _read(HISTORY_ENDPOINT, data, raw=_envelope(list(data))),
        binding=_binding(),
        coordinates=PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=WINDOW_START,
            window_end_at=WINDOW_END,
            historical_backfill=True,
        ),
        normalization_delay_seconds=600,
    )

    assert result.normalization_not_before == RECEIVED_AT + timedelta(minutes=10)
    assert factory.uow.committed is True
    assert any(
        "SET available_at = GREATEST" in statement
        for statement, _params in factory.cursor.calls
    )


@pytest.mark.parametrize(
    ("endpoint", "expected_source_report_calls"),
    ((HISTORY_ENDPOINT, 0), (SLEEP_REPORT_ENDPOINT, 1)),
)
def test_large_pull_reconciliation_commits_in_bounded_transactions(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    expected_source_report_calls: int,
) -> None:
    class ChunkCursor(Cursor):
        def __init__(self) -> None:
            super().__init__(None)
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            super().execute(statement, params)
            self.calls.append((statement, params))

        def fetchone(self) -> tuple[Any, ...] | None:
            if "SELECT work_id" in self.statement:
                return ("work-1",)
            return None

    class ChunkFactory:
        def __init__(self) -> None:
            self.uows: list[Uow] = []

        def begin(self, _scope: object) -> Uow:
            uow = Uow(ChunkCursor())
            self.uows.append(uow)
            return uow

    class Reconciler:
        def reconcile(
            self,
            _cursor: object,
            _scope: object,
            *,
            candidate: int,
            **_kwargs: object,
        ) -> PerceptorReconciliationResult:
            return PerceptorReconciliationResult(
                canonical_observation_id=f"canonical-{candidate:02d}",
                canonical_created=True,
                acquisition_created=True,
                push_pull_overlap=False,
                conflict_created_count=0,
                duplicate=False,
            )

    projected: list[tuple[str, ...]] = []

    def record_projection(
        _connection: object,
        _scope: object,
        observation_ids: tuple[str, ...],
        **_kwargs: object,
    ) -> tuple[object, ...]:
        projected.append(observation_ids)
        return ()

    monkeypatch.setattr(
        pull_ingestion_module,
        "project_authoritative_canonical_observations",
        record_projection,
    )
    monkeypatch.setattr(
        pull_ingestion_module,
        "semantic_surface_for_pull",
        lambda selected_endpoint, _observation: selected_endpoint,
    )
    factory = ChunkFactory()
    processor = PerceptorPullNormalizationProcessor(
        factory,  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="test-key"),
    )
    processor.reconciler = Reconciler()  # type: ignore[assignment]
    source_report_calls: list[str] = []

    def record_source_report(
        _cursor: object,
        _scope: object,
        **_kwargs: object,
    ) -> None:
        source_report_calls.append(endpoint)

    processor._insert_source_report = record_source_report  # type: ignore[method-assign]
    scope = UowScope(
        namespace_id="live:perceptor-pull-test",
        data_mode="live",
        process_role="worker",
        purpose="normalization",
        service_principal_id="worker-1",
        namespace_generation=4,
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-instance-1",
    )
    lease = NormalizationLease(
        work_id="work-1",
        lease_generation=2,
        fencing_token="f" * 32,
        worker_instance="worker-instance-1",
    )
    candidates = tuple(range(17))

    summary = processor._commit_reconciliation(
        scope,
        lease,
        loaded={"raw_ingress_record_id": "raw-1", "work_json": {}},
        normalized={
            "endpoint": endpoint,
            "candidates": candidates,
            "observations": tuple(object() for _ in candidates),
            "canonical_semantics": tuple(None for _ in candidates),
            "no_data": False,
            "history_window_classification": None,
        },
        committed_at=RECEIVED_AT,
    )

    assert [len(chunk) for chunk in projected] == [8, 8, 1]
    assert len(factory.uows) == 4
    assert all(uow.committed for uow in factory.uows)
    progress = [
        json.loads(params[0])["bounded_reconciliation"]
        for uow in factory.uows[:3]
        for statement, params in uow._cursor.calls  # type: ignore[attr-defined]
        if "SET work_json = work_json" in statement
    ]
    assert [item["next_index"] for item in progress] == [8, 16, 17]
    assert source_report_calls == [endpoint] * expected_source_report_calls
    assert summary["canonical_created_count"] == 17
    assert summary["canonical_observation_ids"] == [
        f"canonical-{index:02d}" for index in candidates
    ]
