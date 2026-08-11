from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.fernet import Fernet

from sleepagent.integrations.perceptor import FakePerceptorClient
from sleepagent.integrations.perceptor.pull_ingestion import (
    PerceptorPullCompatibilityProfile,
    PerceptorPullConfigurationError,
    PerceptorPullDevice,
    PerceptorPullError,
    PerceptorPullService,
    partition_device_batches,
    partition_history_windows,
)
from sleepagent.integrations.perceptor.push_ingestion import (
    NONCE_HEADER,
    PROFILE_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_METHOD_HEADER,
    TIMESTAMP_HEADER,
    PerceptorNormalizationWorker,
    PerceptorPushCompatibilityProfile,
    PerceptorPushIngestionService,
    PerceptorRawHttpRequest,
    SignatureMode,
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
    AdapterDeploymentStatus,
    AvailabilityState,
    CandidatePromotionService,
    CasConflictError,
    DataMode,
    DeviceBindingCommand,
    DeviceBindingService,
    DomainNamespace,
    MissingState,
    ObservationType,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    RawPayloadEncryptionPolicy,
    SleepDomainRepository,
    TimezoneStatus,
    ControlledAdapterRegistry,
    perceptor_v1_registration,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
NAMESPACE = DomainNamespace("replay:perceptor-pull-tests", DataMode.REPLAY)
ACCOUNT_ID = "opaque-pull-account"
DEVICE = PerceptorPullDevice(
    device_name="imei-000123",
    home_id="0000456",
    provider_device_id="000123opaque",
    product_id="opaque-product",
)


@dataclass
class PullFixture:
    repository: SleepDomainRepository
    registry: ControlledAdapterRegistry
    client: FakePerceptorClient
    service: PerceptorPullService
    worker: PerceptorNormalizationWorker


@pytest.fixture
def pull() -> PullFixture:
    store = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    repository = SleepDomainRepository(
        store,
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="pull-test-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=7),
            production=False,
        ),
    )
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=NAMESPACE,
            provider_account_id=ACCOUNT_ID,
            provider_id="perceptor",
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"profile": "perceptor-pull-contract.v1"},
            created_at=NOW - timedelta(days=1),
        )
    )
    registration = perceptor_v1_registration(
        configuration_fingerprint="a" * 64,
        adapter_artifact_sha256="b" * 64,
        provider_account_ids=(ACCOUNT_ID,),
        environments=("test",),
    )
    registry = ControlledAdapterRegistry(
        namespace=NAMESPACE,
        repository=repository,
        allowlist=(registration,),
        authorized_human_reviewers=frozenset(),
    )
    assert registry.deployment_status(
        adapter_id="perceptor-v1",
        adapter_version="1.0.0",
    ) == AdapterDeploymentStatus.REGISTERED
    registry.enable(
        adapter_id="perceptor-v1",
        adapter_version="1.0.0",
        deployment_event_id="enable-perceptor-pull-test",
        actor_id="test-deployment",
        reason="test local pull normalization",
        changed_at=NOW - timedelta(hours=1),
    )
    access = AdministrativeAccessPolicy(
        {"admin": frozenset({AdministrativeScope.DEVICE_BINDING_WRITE})},
        authorization_ids={"admin": frozenset({"pull-binding-auth"})},
    )
    DeviceBindingService(repository, access_policy=access).apply(
        NAMESPACE,
        DeviceBindingCommand(
            command_id="bind-pull-device",
            data_mode=DataMode.REPLAY,
            device_binding_id="binding-pull-device",
            expected_binding_version=0,
            device_id="internal-pull-device",
            provider_id="perceptor",
            provider_account_id=ACCOUNT_ID,
            provider_device=ProviderDeviceIdentity(
                provider_device_id=DEVICE.provider_device_id,
                provider_device_name=DEVICE.device_name,
                product_id=DEVICE.product_id,
                home_id=str(DEVICE.home_id),
            ),
            subject_id="subject-pull",
            timezone_name="Asia/Shanghai",
            effective_from=NOW - timedelta(days=2),
            effective_until=None,
            actor_id="admin",
            authorization_id="pull-binding-auth",
            change_reason="test pull binding",
            requested_at=NOW - timedelta(days=1),
        ),
    )
    client = FakePerceptorClient(
        default_device_name=DEVICE.device_name,
        default_home_id=DEVICE.home_id,
    )
    profile = PerceptorPullCompatibilityProfile(
        profile_id="perceptor-pull-contract.v1",
        provider_account_id=ACCOUNT_ID,
        environment="test",
    )
    service = PerceptorPullService(
        namespace=NAMESPACE,
        repository=repository,
        registry=registry,
        client=client,
        profile=profile,
    )
    worker = PerceptorNormalizationWorker(
        namespace=NAMESPACE,
        repository=repository,
        registry=registry,
        promotion_service=CandidatePromotionService(
            repository,
            access_policy=AdministrativeAccessPolicy(
                {},
                authorization_ids={},
            ),
        ),
        worker_id="pull-worker",
        lease_duration=timedelta(seconds=10),
    )
    fixture = PullFixture(repository, registry, client, service, worker)
    yield fixture
    registry.close()
    store.connection.close()


def _run_all(fixture: PullFixture, *, start: datetime = NOW) -> None:
    tick = 1
    while fixture.worker.run_once(now=start + timedelta(seconds=tick)):
        tick += 1


def _report_data(*, score: int = 91) -> dict[str, Any]:
    return {
        "device_name": DEVICE.device_name,
        "report_time": NOW.isoformat(),
        "sleep_stages": [
            {
                "start_time": (NOW - timedelta(hours=1)).isoformat(),
                "end_time": NOW.isoformat(),
                "stage": "deep",
            }
        ],
        "minute_heart_rates": [
            {"time": (NOW - timedelta(minutes=1)).isoformat(), "value": 62},
            {"time": NOW.isoformat(), "value": -1},
        ],
        "minute_respiratory_rates": [
            {"time": NOW.isoformat(), "value": 15},
        ],
        "body_movements": [{"time": NOW.isoformat(), "value": 2}],
        "get_up_events": [{"time": NOW.isoformat()}],
        "profile": {
            "sleep_score": score,
            "sleep_latency": "8-0",
        },
    }


def test_report_version_is_content_addressed_idempotent_empty_and_changed(
    pull: PullFixture,
) -> None:
    pull.client.responses["/vitalSigns/getSleepReport"] = {
        "code": 200,
        "success": True,
        "data": {},
    }
    empty = pull.service.pull_sleep_report(
        DEVICE,
        report_date=date(2026, 7, 29),
        fetched_at=NOW,
    )
    duplicate = pull.service.pull_sleep_report(
        DEVICE,
        report_date=date(2026, 7, 29),
        fetched_at=NOW + timedelta(minutes=1),
    )
    assert empty.changed is True
    assert empty.source_report.is_empty is True
    assert empty.source_report.report_version == 1
    assert duplicate.changed is False
    assert duplicate.intake.created is False
    assert pull.repository.count_rows("sleep_domain_raw_inbox") == 1

    pull.client.responses["/vitalSigns/getSleepReport"]["data"] = _report_data()
    changed = pull.service.pull_sleep_report(
        DEVICE,
        report_date=date(2026, 7, 29),
        fetched_at=NOW + timedelta(minutes=2),
    )
    assert changed.changed is True
    assert changed.source_report.report_version == 2
    assert changed.source_report.is_empty is False
    assert pull.repository.count_rows("sleep_domain_source_reports") == 2
    _run_all(pull, start=NOW + timedelta(minutes=2))
    assert pull.repository.count_rows("sleep_domain_canonical_observations") == 8
    assert pull.repository.count_rows("sleep_domain_night_episodes") == 0
    assert pull.repository.count_rows("sleep_domain_analysis_revisions") == 0


def test_report_normalizes_typed_values_minus_one_and_ambiguous_source_text(
    pull: PullFixture,
) -> None:
    pull.client.responses["/vitalSigns/getSleepReport"] = {
        "code": 200,
        "success": True,
        "data": _report_data(),
    }
    pull.service.pull_sleep_report(
        DEVICE,
        report_date=date(2026, 7, 29),
        fetched_at=NOW,
    )
    _run_all(pull)
    rows = pull.repository.connection.execute(
        "SELECT observation_json FROM sleep_domain_canonical_observations"
    ).fetchall()
    observations = [json.loads(str(row[0])) for row in rows]
    types = [item["observation_type"] for item in observations]
    assert ObservationType.SLEEP_STAGE_INTERVAL.value in types
    assert ObservationType.HEART_RATE.value in types
    assert ObservationType.RESPIRATORY_RATE.value in types
    assert ObservationType.MOVEMENT.value in types
    assert ObservationType.BED_EXIT.value in types
    missing = next(
        item
        for item in observations
        if item["observation_type"] == ObservationType.MISSING_INTERVAL.value
    )
    assert missing["payload"]["missing_state"] == MissingState.INVALID.value
    assert missing["payload"]["target_observation_type"] == "heart_rate"
    ambiguous = next(
        item
        for item in observations
        if item["observation_type"] == "vendor_sleep_profile_metric"
        and item["payload"]["metric_name"] == "sleep_latency"
    )
    assert ambiguous["payload"]["value_state"] == AvailabilityState.UNKNOWN.value
    assert ambiguous["payload"]["value"] is None
    assert ambiguous["payload"]["source_text"] == "8-0"
    respiratory = next(
        item
        for item in observations
        if item["observation_type"] == "respiratory_rate"
    )
    assert respiratory["provenance"]["producer_name"] == "respiratory_rate_series"
    assert "not_signal_artifact_or_ressleepnet_input" in respiratory["quality"][
        "limitations"
    ]


def test_timezone_naive_report_time_is_not_guessed_and_is_quarantined(
    pull: PullFixture,
) -> None:
    data = _report_data()
    data["sleep_stages"] = [
        {
            "start_time": "2026-07-30T05:00:00",
            "end_time": "2026-07-30T06:00:00",
            "stage": "deep",
        }
    ]
    data["minute_heart_rates"] = []
    data["minute_respiratory_rates"] = []
    data["body_movements"] = []
    data["get_up_events"] = []
    data["profile"] = {}
    data.pop("report_time")
    pull.client.responses["/vitalSigns/getSleepReport"] = {
        "code": 200,
        "success": True,
        "data": data,
    }
    pull.service.pull_sleep_report(
        DEVICE,
        report_date=date(2026, 7, 29),
        fetched_at=NOW,
    )
    _run_all(pull)
    assert pull.repository.count_rows("sleep_domain_canonical_observations") == 0
    assert pull.repository.count_rows("sleep_domain_quarantine") == 1
    candidate_row = pull.repository.connection.execute(
        "SELECT candidate_json FROM sleep_domain_adapter_candidates"
    ).fetchone()
    candidate = json.loads(str(candidate_row[0]))
    assert candidate["timezone_status"] == TimezoneStatus.TIMEZONE_UNKNOWN.value
    assert "2026-07-30T05:00:00" in candidate["source_timestamp_text"]


def test_history_reconstructs_backward_marks_quality_and_never_uses_minus_one(
    pull: PullFixture,
) -> None:
    pull.client.responses["/vitalSigns/getHistoryData"] = {
        "code": 200,
        "success": True,
        "data": [
            {
                "device_name": DEVICE.device_name,
                "send_time": NOW.isoformat(),
                "heart_rate": "60,61",
                "respiratory_rate": "14,-1",
                "movement": "0,2",
            }
        ],
    }
    result = pull.service.reconcile_history(
        [DEVICE],
        start_at=NOW - timedelta(minutes=5),
        end_at=NOW,
        stream_key="device-history",
        reconciled_at=NOW + timedelta(minutes=1),
    )
    assert result.checkpoint is not None
    assert result.checkpoint.cursor_at == NOW
    _run_all(pull, start=NOW + timedelta(minutes=1))
    rows = pull.repository.connection.execute(
        "SELECT observation_json FROM sleep_domain_canonical_observations"
    ).fetchall()
    observations = [json.loads(str(row[0])) for row in rows]
    assert len(observations) == 6
    times = sorted(
        item["measurement_at"]
        for item in observations
        if item["measurement_at"] is not None
    )
    assert datetime.fromisoformat(times[0].replace("Z", "+00:00")) == (
        NOW - timedelta(seconds=3)
    )
    assert datetime.fromisoformat(times[-1].replace("Z", "+00:00")) == NOW
    assert all(
        "reconstructed_time" in item["quality"]["quality_flags"]
        for item in observations
    )
    sentinel = next(
        item
        for item in observations
        if item["observation_type"] == "missing_interval"
    )
    assert sentinel["payload"]["target_observation_type"] == "respiratory_rate"
    assert "-1" not in {
        str(item["payload"].get("value"))
        for item in observations
        if isinstance(item["payload"], dict)
    }


def test_history_partition_limits_and_checkpoint_recovery(pull: PullFixture) -> None:
    devices = [
        PerceptorPullDevice(device_name=f"device-{index}", home_id="home")
        for index in range(21)
    ]
    assert len(
        partition_history_windows(
            NOW,
            NOW + timedelta(hours=2, minutes=1),
        )
    ) == 3
    assert [len(batch) for batch in partition_device_batches(devices)] == [20, 1]

    class FailingClient(FakePerceptorClient):
        def __init__(self) -> None:
            super().__init__(default_home_id="home")
            self.history_calls = 0
            self.fail = True

        def get_history_data(self, **kwargs: Any) -> dict[str, Any]:
            self.history_calls += 1
            if self.fail and self.history_calls == 2:
                raise TimeoutError("provider timeout")
            names = kwargs["device_names"]
            return {
                "code": 200,
                "success": True,
                "data": [
                    {
                        "device_name": name,
                        "send_time": NOW.isoformat(),
                        "heart_rate": "",
                        "respiratory_rate": "",
                        "movement": "",
                    }
                    for name in names
                ],
            }

    client = FailingClient()
    service = PerceptorPullService(
        namespace=NAMESPACE,
        repository=pull.repository,
        registry=pull.registry,
        client=client,
        profile=pull.service.profile,
    )
    with pytest.raises(TimeoutError):
        service.reconcile_history(
            devices,
            start_at=NOW,
            end_at=NOW + timedelta(minutes=30),
            stream_key="recovery",
            reconciled_at=NOW + timedelta(hours=1),
        )
    assert pull.repository.get_pull_checkpoint(
        NAMESPACE,
        provider_id="perceptor",
        provider_account_id=ACCOUNT_ID,
        stream_key="recovery",
    ) is None
    assert pull.repository.count_rows("sleep_domain_raw_inbox") == 1

    client.fail = False
    recovered = service.reconcile_history(
        devices,
        start_at=NOW,
        end_at=NOW + timedelta(minutes=30),
        stream_key="recovery",
        reconciled_at=NOW + timedelta(hours=1, minutes=1),
    )
    assert recovered.checkpoint is not None
    assert recovered.checkpoint.cursor_at == NOW + timedelta(minutes=30)
    assert recovered.intake_results[0].created is False
    assert recovered.intake_results[1].created is True
    with pytest.raises(CasConflictError):
        pull.repository.advance_pull_checkpoint(
            NAMESPACE,
            checkpoint_id=recovered.checkpoint.checkpoint_id,
            provider_id="perceptor",
            provider_account_id=ACCOUNT_ID,
            stream_key="recovery",
            cursor_at=recovered.checkpoint.cursor_at,
            lateness_watermark_at=recovered.checkpoint.lateness_watermark_at,
            expected_cas_version=recovered.checkpoint.cas_version - 1,
            updated_at=NOW + timedelta(hours=1, minutes=2),
        )


def test_unequal_history_series_persists_raw_but_does_not_advance_checkpoint(
    pull: PullFixture,
) -> None:
    pull.client.responses["/vitalSigns/getHistoryData"] = {
        "code": 200,
        "success": True,
        "data": [
            {
                "device_name": DEVICE.device_name,
                "send_time": NOW.isoformat(),
                "heart_rate": "60,61",
                "respiratory_rate": "14",
                "movement": "0,1",
            }
        ],
    }
    with pytest.raises(PerceptorPullError, match="lengths"):
        pull.service.reconcile_history(
            [DEVICE],
            start_at=NOW - timedelta(minutes=5),
            end_at=NOW,
            stream_key="invalid-series",
            reconciled_at=NOW,
        )
    assert pull.repository.count_rows("sleep_domain_raw_inbox") == 1
    assert pull.repository.get_pull_checkpoint(
        NAMESPACE,
        provider_id="perceptor",
        provider_account_id=ACCOUNT_ID,
        stream_key="invalid-series",
    ) is None
    _run_all(pull)
    assert pull.repository.count_rows("sleep_domain_quarantine") == 1


def test_fallback_is_explicitly_configured_and_uses_unified_inbox(
    pull: PullFixture,
) -> None:
    with pytest.raises(PerceptorPullConfigurationError, match="disabled"):
        pull.service.run_realtime_fallback(
            DEVICE,
            gap_key="gap-1",
            fetched_at=NOW,
        )
    assert pull.client.requests == []

    enabled = PerceptorPullService(
        namespace=NAMESPACE,
        repository=pull.repository,
        registry=pull.registry,
        client=pull.client,
        profile=PerceptorPullCompatibilityProfile(
            profile_id="perceptor-pull-contract.v1",
            provider_account_id=ACCOUNT_ID,
            environment="test",
            realtime_fallback_enabled=True,
        ),
    )
    enabled.run_realtime_fallback(
        DEVICE,
        gap_key="gap-1",
        fetched_at=NOW,
    )
    assert [item["endpoint"] for item in pull.client.requests] == [
        "/vitalSigns/start",
        "/vitalSigns/getRealTimes",
    ]
    assert pull.repository.count_rows("sleep_domain_raw_inbox") == 1


def test_push_pull_same_fact_links_acquisitions_and_changed_value_conflicts(
    pull: PullFixture,
) -> None:
    push_profile = PerceptorPushCompatibilityProfile(
        profile_id="perceptor-push-contract.v1",
        provider_account_id=ACCOUNT_ID,
        signing_secret="push-test-secret",
        signature_algorithm="HMAC-SHA256",
        signature_mode=SignatureMode.RAW_BODY,
        signing_path="/perceptor/push",
        append_ampersand_to_secret=False,
        timestamp_window=timedelta(minutes=5),
        future_clock_skew=timedelta(seconds=30),
        environment="test",
    )
    push_service = PerceptorPushIngestionService(
        namespace=NAMESPACE,
        repository=pull.repository,
        registry=pull.registry,
        profiles={push_profile.profile_id: push_profile},
    )
    push_payload = {
        "message_id": "same-fact-push",
        "device_id": DEVICE.provider_device_id,
        "device_name": DEVICE.device_name,
        "product_id": DEVICE.product_id,
        "home_id": str(DEVICE.home_id),
        "type": "VitalSignsDataEvent",
        "data": {
            "DateTime": int(NOW.timestamp() * 1000),
            "HeartRate": 70,
            "BreathRate": 15,
            "BodyShake": 2,
            "OnBed": 1,
        },
    }
    body = json.dumps(push_payload, separators=(",", ":")).encode()
    signature = sign_perceptor_push_request(body, profile=push_profile)
    request = PerceptorRawHttpRequest(
        method="POST",
        path="/perceptor/push",
        raw_headers=(
            (b"content-type", b"application/json"),
            (PROFILE_HEADER.encode(), push_profile.profile_id.encode()),
            (SIGNATURE_METHOD_HEADER.encode(), b"HMAC-SHA256"),
            (TIMESTAMP_HEADER.encode(), NOW.isoformat().encode()),
            (NONCE_HEADER.encode(), b"same-fact-nonce"),
            (SIGNATURE_HEADER.encode(), signature.encode()),
        ),
        body=body,
        client_key="same-fact-test",
    )
    push_service.ingest(request, received_at=NOW)
    _run_all(pull)
    assert pull.repository.count_rows("sleep_domain_canonical_observations") == 4

    pull.client.responses["/vitalSigns/getHistoryData"] = {
        "code": 200,
        "success": True,
        "data": [
            {
                "device_name": DEVICE.device_name,
                "send_time": NOW.isoformat(),
                "heart_rate": "70",
                "respiratory_rate": "15",
                "movement": "2",
            }
        ],
    }
    pull.service.reconcile_history(
        [DEVICE],
        start_at=NOW - timedelta(minutes=5),
        end_at=NOW,
        stream_key="same-fact",
        reconciled_at=NOW + timedelta(minutes=1),
    )
    _run_all(pull, start=NOW + timedelta(minutes=1))
    assert pull.repository.count_rows("sleep_domain_canonical_observations") == 4
    assert pull.repository.count_rows("sleep_domain_observation_acquisitions") == 7
    assert pull.repository.count_rows("sleep_domain_observation_conflicts") == 0

    pull.client.responses["/vitalSigns/getHistoryData"]["data"][0][
        "heart_rate"
    ] = "71"
    pull.service.reconcile_history(
        [DEVICE],
        start_at=NOW - timedelta(minutes=5),
        end_at=NOW,
        stream_key="same-fact",
        reconciled_at=NOW + timedelta(minutes=2),
    )
    _run_all(pull, start=NOW + timedelta(minutes=2))
    assert pull.repository.count_rows("sleep_domain_canonical_observations") == 5
    assert pull.repository.count_rows("sleep_domain_observation_acquisitions") == 10
    assert pull.repository.count_rows("sleep_domain_observation_conflicts") == 1


def test_pull_migration_is_additive_and_does_not_create_episode_logic() -> None:
    assert SCHEMA_VERSION == "001_initial_schema"
    sql = POSTGRES_BASELINE_SQL
    for table in (
        "sleep_domain_source_reports",
        "sleep_domain_pull_checkpoints",
        "sleep_domain_observation_fact_values",
        "sleep_domain_observation_acquisitions",
        "sleep_domain_observation_conflicts",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "ON DELETE RESTRICT" in sql
