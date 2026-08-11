from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from sleepagent.integrations.perceptor.push_ingestion import (
    NONCE_HEADER,
    PROFILE_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_METHOD_HEADER,
    TIMESTAMP_HEADER,
    PerceptorNormalizationWorker,
    PerceptorPushAuthenticationError,
    PerceptorPushCompatibilityProfile,
    PerceptorPushHttpLimits,
    PerceptorPushIngestionService,
    PerceptorPushReplayError,
    PerceptorPushRequestTooLarge,
    PerceptorRawHttpRequest,
    SignatureMode,
    read_bounded_starlette_request,
    sign_perceptor_push_request,
)
from sleepagent.persistence import RadarPersistenceStore
from sleepagent.persistence import (
    SCHEMA_VERSION,
    POSTGRES_BASELINE_SQL,
)
from sleepagent.sleep_domain import (
    AdministrativeAccessPolicy,
    AdministrativeScope,
    AdapterCapability,
    AdapterDeploymentStatus,
    CandidatePromotionService,
    ControlledAdapterRegistry,
    DataMode,
    DeviceBindingCommand,
    DeviceBindingService,
    DomainNamespace,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    RawPayloadEncryptionPolicy,
    SleepDomainRepository,
    perceptor_v1_registration,
)


NOW = datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)
NAMESPACE = DomainNamespace("replay:perceptor-push-tests", DataMode.REPLAY)
ACCOUNT_ID = "opaque-provider-account"
PROFILE_ID = "perceptor-push-contract.v1"


@dataclass
class PushFixture:
    repository: SleepDomainRepository
    registry: ControlledAdapterRegistry
    service: PerceptorPushIngestionService
    worker: PerceptorNormalizationWorker
    profile: PerceptorPushCompatibilityProfile


@pytest.fixture
def push() -> PushFixture:
    store = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    repository = SleepDomainRepository(
        store,
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="test-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=7),
            production=False,
        ),
    )
    registration = perceptor_v1_registration(
        configuration_fingerprint="a" * 64,
        adapter_artifact_sha256="b" * 64,
        provider_account_ids=(ACCOUNT_ID,),
        environments=("test",),
    )
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=NAMESPACE,
            provider_account_id=ACCOUNT_ID,
            provider_id="perceptor",
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"profile": PROFILE_ID},
            created_at=NOW - timedelta(days=1),
        )
    )
    registry = ControlledAdapterRegistry(
        namespace=NAMESPACE,
        repository=repository,
        allowlist=(registration,),
        authorized_human_reviewers=frozenset(),
    )
    assert (
        registry.deployment_status(
            adapter_id="perceptor-v1",
            adapter_version="1.0.0",
        )
        == AdapterDeploymentStatus.REGISTERED
    )
    registry.enable(
        adapter_id="perceptor-v1",
        adapter_version="1.0.0",
        deployment_event_id="enable-perceptor-test",
        actor_id="test-deployment",
        reason="test local normalization implementation",
        changed_at=NOW - timedelta(hours=1),
    )
    profile = PerceptorPushCompatibilityProfile(
        profile_id=PROFILE_ID,
        provider_account_id=ACCOUNT_ID,
        signing_secret="test-signing-secret",
        signature_algorithm="HMAC-SHA256",
        signature_mode=SignatureMode.RAW_BODY,
        signing_path="/integrations/perceptor/webhook",
        append_ampersand_to_secret=False,
        timestamp_window=timedelta(minutes=5),
        future_clock_skew=timedelta(seconds=30),
        environment="test",
    )
    service = PerceptorPushIngestionService(
        namespace=NAMESPACE,
        repository=repository,
        registry=registry,
        profiles={PROFILE_ID: profile},
        http_limits=PerceptorPushHttpLimits(
            max_body_bytes=4096,
            max_header_count=16,
            max_header_bytes=4096,
        ),
    )
    promotion = CandidatePromotionService(
        repository,
        access_policy=AdministrativeAccessPolicy({}, authorization_ids={}),
    )
    worker = PerceptorNormalizationWorker(
        namespace=NAMESPACE,
        repository=repository,
        registry=registry,
        promotion_service=promotion,
        worker_id="perceptor-worker-test",
        lease_duration=timedelta(seconds=5),
    )
    fixture = PushFixture(repository, registry, service, worker, profile)
    yield fixture
    registry.close()
    store.connection.close()


def _payload(
    *,
    message_id: str = "opaque-message-1",
    data: object | None = None,
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "product_id": "opaque-product",
        "device_id": "000123opaque",
        "device_name": "imei-000123",
        "home_id": "0000456",
        "type": "VitalSignsDataEvent",
        "data": data
        if data is not None
        else {
            "DateTime": int(NOW.timestamp() * 1000),
            "HeartRate": 70,
            "BreathRate": 15,
            "BodyShake": 2,
            "OnBed": 1,
        },
    }


def _request(
    push: PushFixture,
    payload: dict[str, object] | bytes,
    *,
    nonce: str = "nonce-1",
    signed_at: datetime = NOW,
    signature_method: str = "HMAC-SHA256",
    signature: str | None = None,
) -> PerceptorRawHttpRequest:
    body = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    supplied_signature = signature or sign_perceptor_push_request(
        body,
        profile=push.profile,
    )
    headers = (
        (b"content-type", b"application/json"),
        (PROFILE_HEADER.encode(), PROFILE_ID.encode()),
        (SIGNATURE_METHOD_HEADER.encode(), signature_method.encode()),
        (TIMESTAMP_HEADER.encode(), signed_at.isoformat().encode()),
        (NONCE_HEADER.encode(), nonce.encode()),
        (SIGNATURE_HEADER.encode(), supplied_signature.encode()),
    )
    return PerceptorRawHttpRequest(
        method="POST",
        path="/integrations/perceptor/webhook",
        raw_headers=headers,
        body=body,
        client_key="test-client",
    )


@pytest.mark.parametrize(
    "data",
    [
        {
            "DateTime": int(NOW.timestamp() * 1000),
            "HeartRate": 70,
            "BreathRate": 15,
            "BodyShake": 2,
            "Onbed": 1,
        },
        json.dumps(
            {
                "DateTime": str(int(NOW.timestamp() * 1000)),
                "HeartRate": "70",
                "BreathRate": "15",
                "BodyShake": "2",
                "OnBed": "1",
            }
        ),
    ],
)
def test_supported_object_and_escaped_string_formats_are_normalized_locally(
    push: PushFixture,
    data: object,
) -> None:
    result = push.service.ingest(
        _request(push, _payload(data=data)),
        received_at=NOW,
    )
    assert result.normalization_status == "pending"
    assert push.worker.run_once(now=NOW + timedelta(seconds=1)) is True
    assert push.repository.count_rows("sleep_domain_adapter_candidates") == 4
    assert push.repository.count_rows("sleep_domain_quarantine") == 4
    assert push.repository.count_rows("sleep_domain_canonical_observations") == 0


@pytest.mark.parametrize(
    "source_time",
    [
        int(NOW.timestamp()),
        str(int(NOW.timestamp())),
        NOW.isoformat(),
        NOW.isoformat().replace("+00:00", "Z"),
    ],
)
def test_seconds_numeric_string_and_iso_source_times_are_supported(
    push: PushFixture,
    source_time: object,
) -> None:
    data = {
        "DateTime": source_time,
        "HeartRate": "70",
        "BreathRate": "15",
        "BodyShake": "2",
        "OnBed": 1,
    }
    push.service.ingest(
        _request(push, _payload(data=data), nonce=f"time-{source_time}"),
        received_at=NOW,
    )
    assert push.worker.run_once(now=NOW + timedelta(seconds=1)) is True
    assert push.repository.count_rows("sleep_domain_adapter_candidates") == 4


def test_invalid_signature_algorithm_timestamp_and_nonce_fail_closed(
    push: PushFixture,
) -> None:
    with pytest.raises(PerceptorPushAuthenticationError):
        push.service.ingest(
            _request(push, _payload(), signature="bad"),
            received_at=NOW,
        )
    with pytest.raises(PerceptorPushAuthenticationError):
        push.service.ingest(
            _request(push, _payload(), signature_method="HMAC-SHA1"),
            received_at=NOW,
        )
    with pytest.raises(PerceptorPushAuthenticationError):
        push.service.ingest(
            _request(
                push,
                _payload(),
                nonce="stale",
                signed_at=NOW - timedelta(minutes=6),
            ),
            received_at=NOW,
        )
    with pytest.raises(PerceptorPushAuthenticationError):
        push.service.ingest(
            _request(
                push,
                _payload(),
                nonce="future",
                signed_at=NOW + timedelta(seconds=31),
            ),
            received_at=NOW,
        )
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 0
    assert push.repository.count_rows("sleep_domain_ingress_nonces") == 0


def test_nonce_replay_rejects_different_request_but_exact_duplicate_acks(
    push: PushFixture,
) -> None:
    first = push.service.ingest(
        _request(push, _payload(), nonce="same-nonce"),
        received_at=NOW,
    )
    duplicate = push.service.ingest(
        _request(push, _payload(), nonce="same-nonce"),
        received_at=NOW + timedelta(seconds=1),
    )
    assert first.duplicate is False
    assert duplicate.duplicate is True
    with pytest.raises(PerceptorPushReplayError):
        push.service.ingest(
            _request(
                push,
                _payload(message_id="another-message"),
                nonce="same-nonce",
            ),
            received_at=NOW + timedelta(seconds=2),
        )
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 1
    assert push.repository.count_rows("sleep_domain_ingress_nonces") == 1


def test_same_message_id_different_pre_normalization_hash_is_quarantined_and_acked(
    push: PushFixture,
) -> None:
    first = push.service.ingest(
        _request(push, _payload(), nonce="collision-1"),
        received_at=NOW,
    )
    changed = _payload()
    assert isinstance(changed["data"], dict)
    changed["data"]["HeartRate"] = 99
    result = push.service.ingest(
        _request(push, changed, nonce="collision-2"),
        received_at=NOW + timedelta(seconds=1),
    )
    assert result.accepted is True
    assert result.collision is True
    assert result.normalization_status == "quarantined"
    retried_collision = push.service.ingest(
        _request(push, changed, nonce="collision-2"),
        received_at=NOW + timedelta(seconds=2),
    )
    assert retried_collision.duplicate is True
    assert retried_collision.collision is True
    assert retried_collision.normalization_status == "quarantined"
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 2
    assert push.repository.count_rows("sleep_domain_quarantine") == 1
    assert push.repository.count_rows("sleep_domain_processing_outbox") == 2
    only_lease = push.repository.lease_normalization_work(
        NAMESPACE,
        worker_id="must-not-lease-collision",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=5),
    )
    assert only_lease is not None
    assert only_lease.raw_ingress_record_id == first.raw_ingress_record_id
    assert push.repository.lease_normalization_work(
        NAMESPACE,
        worker_id="must-not-lease-collision-2",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=5),
    ) is None
    event_types = {
        row[0]
        for row in push.repository.connection.execute(
            "SELECT event_type FROM sleep_domain_processing_outbox"
        ).fetchall()
    }
    assert event_types == {"RAW_ACCEPTED", "MESSAGE_ID_COLLISION"}


def test_canonical_parameter_hmac_profile_is_explicit_and_enforced(
    push: PushFixture,
) -> None:
    profile = PerceptorPushCompatibilityProfile(
        profile_id="perceptor-canonical.v1",
        provider_account_id=ACCOUNT_ID,
        signing_secret="canonical-secret",
        signature_algorithm="HMAC-SHA1",
        signature_mode=SignatureMode.CANONICAL_PARAMETERS,
        signing_path="/receive",
        append_ampersand_to_secret=True,
        timestamp_window=timedelta(minutes=5),
        future_clock_skew=timedelta(seconds=30),
        environment="test",
    )
    service = PerceptorPushIngestionService(
        namespace=NAMESPACE,
        repository=push.repository,
        registry=push.registry,
        profiles={profile.profile_id: profile},
    )
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    request = PerceptorRawHttpRequest(
        method="POST",
        path="/receive",
        raw_headers=(
            (PROFILE_HEADER.encode(), profile.profile_id.encode()),
            (SIGNATURE_METHOD_HEADER.encode(), b"HMAC-SHA1"),
            (TIMESTAMP_HEADER.encode(), NOW.isoformat().encode()),
            (NONCE_HEADER.encode(), b"canonical-nonce"),
            (
                SIGNATURE_HEADER.encode(),
                sign_perceptor_push_request(body, profile=profile).encode(),
            ),
        ),
        body=body,
        client_key="canonical-client",
    )
    result = service.ingest(request, received_at=NOW)
    assert result.accepted is True
    assert result.compatibility_profile_id == profile.profile_id


def test_valid_signed_malformed_body_is_encrypted_quarantined_and_acked(
    push: PushFixture,
) -> None:
    body = b'{"message_id":"malformed",'
    request = _request(push, body, nonce="malformed")
    result = push.service.ingest(request, received_at=NOW)
    assert result.accepted is True
    assert result.normalization_status == "quarantined"
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 1
    assert push.repository.count_rows("sleep_domain_quarantine") == 1
    assert push.repository.lease_normalization_work(
        NAMESPACE,
        worker_id="must-not-lease-malformed",
        now=NOW + timedelta(seconds=1),
        lease_duration=timedelta(seconds=5),
    ) is None
    assert (
        push.repository.load_raw_payload(
            NAMESPACE,
            raw_ingress_record_id=result.raw_ingress_record_id,
        )
        == body
    )
    ciphertext = push.repository.connection.execute(
        "SELECT encrypted_payload FROM sleep_domain_raw_inbox"
    ).fetchone()[0]
    assert body not in bytes(ciphertext)


def test_valid_signed_nonopaque_external_id_is_quarantined_not_auth_rejected(
    push: PushFixture,
) -> None:
    payload = _payload()
    payload["message_id"] = 123
    result = push.service.ingest(
        _request(push, payload, nonce="numeric-id"),
        received_at=NOW,
    )
    assert result.accepted is True
    assert result.normalization_status == "quarantined"
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 1
    assert push.repository.count_rows("sleep_domain_quarantine") == 1


def test_valid_signed_nested_malformed_payload_is_acked_then_worker_quarantines(
    push: PushFixture,
) -> None:
    result = push.service.ingest(
        _request(
            push,
            _payload(data="{not-json"),
            nonce="nested-malformed",
        ),
        received_at=NOW,
    )
    assert result.accepted is True
    assert result.normalization_status == "pending"
    assert push.worker.run_once(now=NOW + timedelta(seconds=1)) is True
    assert push.repository.count_rows("sleep_domain_quarantine") == 1
    assert push.repository.count_rows("sleep_domain_canonical_observations") == 0


@pytest.mark.parametrize(
    "event_type,data",
    [
        ("ConnectedEvent", {}),
        ("DisconnectedEvent", {}),
        (
            "AlarmEvent",
            {"DateTime": int(NOW.timestamp() * 1000), "alarmCode": "opaque-1"},
        ),
        (
            "AlarmStopEvent",
            {"DateTime": int(NOW.timestamp() * 1000), "alarmCode": "opaque-1"},
        ),
    ],
)
def test_connectivity_and_alert_formats_remain_typed_unbound_candidates(
    push: PushFixture,
    event_type: str,
    data: dict[str, object],
) -> None:
    payload = _payload(message_id=f"message-{event_type}", data=data)
    payload["type"] = event_type
    push.service.ingest(
        _request(push, payload, nonce=f"nonce-{event_type}"),
        received_at=NOW,
    )
    assert push.worker.run_once(now=NOW + timedelta(seconds=1)) is True
    assert push.repository.count_rows("sleep_domain_adapter_candidates") == 1
    assert push.repository.count_rows("sleep_domain_quarantine") == 1


def test_body_and_header_limits_apply_before_json_normalization(
    push: PushFixture,
) -> None:
    oversized = _request(push, b"x" * 4097, nonce="oversized")
    with pytest.raises(PerceptorPushRequestTooLarge):
        push.service.ingest(oversized, received_at=NOW)
    too_many_headers = oversized.__class__(
        method="POST",
        path=oversized.path,
        raw_headers=oversized.raw_headers + ((b"x", b"y"),) * 20,
        body=b"{}",
        client_key="test-client",
    )
    with pytest.raises(PerceptorPushRequestTooLarge):
        push.service.ingest(too_many_headers, received_at=NOW)
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 0


def test_concurrent_exact_delivery_commits_one_raw_work_and_intent(
    push: PushFixture,
) -> None:
    def ingest(index: int):
        return push.service.ingest(
            _request(push, _payload(), nonce=f"concurrent-{index}"),
            received_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(ingest, range(8)))
    assert sum(not result.duplicate for result in results) == 1
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 1
    assert push.repository.count_rows("sleep_domain_normalization_work") == 1
    assert push.repository.count_rows("sleep_domain_processing_outbox") == 1
    assert push.repository.count_rows("sleep_domain_ingress_nonces") == 8


def test_expired_worker_lease_is_reclaimed_without_duplicate_candidates(
    push: PushFixture,
) -> None:
    push.service.ingest(
        _request(push, _payload(), nonce="crash-recovery"),
        received_at=NOW,
    )
    crashed = push.repository.lease_normalization_work(
        NAMESPACE,
        worker_id="crashed-worker",
        now=NOW,
        lease_duration=timedelta(seconds=1),
    )
    assert crashed is not None
    assert push.worker.run_once(now=NOW + timedelta(seconds=2)) is True
    assert push.repository.count_rows("sleep_domain_adapter_candidates") == 4
    assert push.repository.count_rows("sleep_domain_quarantine") == 4
    assert push.worker.run_once(now=NOW + timedelta(seconds=3)) is False


def test_bound_trustworthy_measurements_become_canonical_without_raw_payload(
    push: PushFixture,
) -> None:
    policy = AdministrativeAccessPolicy(
        {"admin": frozenset({AdministrativeScope.DEVICE_BINDING_WRITE})},
        authorization_ids={"admin": frozenset({"binding-auth"})},
    )
    DeviceBindingService(push.repository, access_policy=policy).apply(
        NAMESPACE,
        DeviceBindingCommand(
            command_id="bind-before-push",
            data_mode=DataMode.REPLAY,
            device_binding_id="binding-1",
            expected_binding_version=0,
            device_id="internal-device-1",
            provider_id="perceptor",
            provider_account_id=ACCOUNT_ID,
            provider_device=ProviderDeviceIdentity(
                provider_device_id="000123opaque",
                provider_device_name="imei-000123",
                product_id="opaque-product",
                home_id="0000456",
            ),
            subject_id="elder-subject-1",
            timezone_name="Asia/Shanghai",
            effective_from=NOW - timedelta(hours=1),
            actor_id="admin",
            authorization_id="binding-auth",
            change_reason="explicit test binding",
            requested_at=NOW - timedelta(hours=2),
        ),
    )
    result = push.service.ingest(
        _request(push, _payload(), nonce="bound"),
        received_at=NOW,
    )
    assert push.worker.run_once(now=NOW + timedelta(seconds=1)) is True
    assert push.repository.count_rows("sleep_domain_canonical_observations") == 4
    assert push.repository.count_rows("sleep_domain_quarantine") == 0
    rows = push.repository.connection.execute(
        "SELECT observation_json FROM sleep_domain_canonical_observations"
    ).fetchall()
    serialized = "\n".join(str(row[0]) for row in rows)
    assert "elder-subject-1" in serialized
    assert '"raw_payload"' not in serialized
    assert "test-signing-secret" not in serialized
    raw = push.repository.load_raw_payload(
        NAMESPACE,
        raw_ingress_record_id=result.raw_ingress_record_id,
    )
    assert raw == _request(push, _payload(), nonce="ignored").body


def test_ack_result_is_not_created_when_atomic_intake_fails(
    push: PushFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_intake(*args, **kwargs):
        raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(push.repository, "intake_raw", fail_intake)
    with pytest.raises(sqlite3.OperationalError):
        push.service.ingest(
            _request(push, _payload(), nonce="commit-failure"),
            received_at=NOW,
        )


def test_terminal_quarantine_failure_rolls_back_raw_work_and_outbox(
    push: PushFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_terminal(*args, **kwargs):
        raise sqlite3.OperationalError("simulated quarantine failure")

    monkeypatch.setattr(
        push.repository,
        "_terminally_quarantine_work",
        fail_terminal,
    )
    with pytest.raises(sqlite3.OperationalError):
        push.service.ingest(
            _request(push, b'{"message_id":"broken",', nonce="rollback"),
            received_at=NOW,
        )
    assert push.repository.count_rows("sleep_domain_raw_inbox") == 0
    assert push.repository.count_rows("sleep_domain_normalization_work") == 0
    assert push.repository.count_rows("sleep_domain_processing_outbox") == 0
    assert push.repository.count_rows("sleep_domain_quarantine") == 0


def test_starlette_reader_preserves_exact_body_and_raw_headers_before_parse() -> None:
    body = b'{ "message_id" : "opaque", "data" : "{\\"Onbed\\":1}" }'
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/integrations/perceptor/webhook",
            "query_string": b"",
            "headers": [(b"x-exact-case", b" exact-value ")],
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("test", 80),
        },
        receive,
    )
    preserved = asyncio.run(
        read_bounded_starlette_request(
            request,
            limits=PerceptorPushHttpLimits(),
        )
    )
    assert preserved.body == body
    assert preserved.raw_headers == ((b"x-exact-case", b" exact-value "),)


def test_ack_payload_and_durable_intent_do_not_echo_raw_or_security_values(
    push: PushFixture,
) -> None:
    request = _request(push, _payload(), nonce="private-ack")
    result = push.service.ingest(request, received_at=NOW)
    response = result.to_response_payload()
    serialized = json.dumps(response)
    assert result.raw_ingress_record_id not in serialized
    assert "opaque-message-1" not in serialized
    assert "000123opaque" not in serialized
    intent = push.repository.connection.execute(
        "SELECT intent_json FROM sleep_domain_processing_outbox"
    ).fetchone()[0]
    assert "raw_headers_sha256" in str(intent)
    assert "test-signing-secret" not in str(intent)
    assert request.header(SIGNATURE_HEADER) not in str(intent)


def test_perceptor_capability_remains_pending_even_with_local_adapter(
    push: PushFixture,
) -> None:
    descriptor = push.registry._registrations[
        ("perceptor-v1", "1.0.0")
    ].descriptor
    vital_push = [
        item
        for item in descriptor.capabilities
        if item.capability == AdapterCapability.VITAL_PUSH
        and item.environment == "test"
    ]
    assert len(vital_push) == 1
    assert vital_push[0].verification_status.value == "pending"


def test_push_nonce_migration_is_additive_and_restricts_raw_deletion() -> None:
    assert SCHEMA_VERSION == "001_initial_schema"
    sql = POSTGRES_BASELINE_SQL
    assert "CREATE TABLE IF NOT EXISTS sleep_domain_ingress_nonces" in sql
    assert "ON DELETE RESTRICT" in sql
