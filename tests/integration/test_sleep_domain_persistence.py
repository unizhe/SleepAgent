from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from sleepagent.persistence import (
    SCHEMA_VERSION,
    POSTGRES_BASELINE_SQL,
    RadarPersistenceStore,
    apply_sqlite_schema,
    split_sql_statements,
)
from sleepagent.sleep_domain import (
    AdapterCapability,
    AdapterDeploymentStatus,
    AdapterDescriptor,
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AnalysisRevision,
    AvailabilityState,
    CalibrationValue,
    CapabilityDeclaration,
    CapabilitySupport,
    CapabilityVerificationStatus,
    CollectionWindowDerivation,
    ConfidenceValue,
    DataMode,
    DataSufficiency,
    DeviceBinding,
    DeviceBindingOverlapError,
    DeviceBindingReference,
    DeviceBindingStatus,
    DomainEvent,
    DomainEventType,
    DomainNamespace,
    HeartRatePayload,
    IdempotencyConflictError,
    ImmutableRecordConflictError,
    MissingState,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeState,
    NightRevisionCause,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    Operation,
    OperationStatus,
    ProcessingOutcome,
    ProcessingReceipt,
    ProcessingStage,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    QuarantineReason,
    RawIngressRecord,
    RawPayloadConfigurationError,
    RawPayloadDecryptionError,
    RawPayloadEncryptionPolicy,
    SignatureVerificationState,
    SleepDomainRepository,
    SleepObservation,
    SourceKind,
    TimezoneStatus,
    VerificationReviewerKind,
    bind_adapter_candidate,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
LIVE = DomainNamespace("live:tenant-a", DataMode.LIVE)
REPLAY = DomainNamespace("replay:test-suite-a", DataMode.REPLAY)
CONFIG_SHA = "a" * 64
OBSERVATION_SET_SHA = "b" * 64


def _policy(
    *,
    key: bytes | None = None,
    retention: timedelta = timedelta(minutes=15),
) -> RawPayloadEncryptionPolicy:
    return RawPayloadEncryptionPolicy(
        key_id="test-key-v1",
        key=key or Fernet.generate_key(),
        retention_period=retention,
        production=False,
    )


def _repository(
    connection: sqlite3.Connection | None = None,
    *,
    policy: RawPayloadEncryptionPolicy | None = None,
) -> tuple[SleepDomainRepository, sqlite3.Connection]:
    connection = connection or sqlite3.connect(":memory:", check_same_thread=False)
    store = RadarPersistenceStore.connect_sqlite(connection)
    return (
        SleepDomainRepository(
            store,
            raw_payload_policy=policy or _policy(),
        ),
        connection,
    )


def _provider_account(
    namespace: DomainNamespace,
    *,
    provider_account_id: str = "account-1",
) -> ProviderAccountRecord:
    return ProviderAccountRecord(
        namespace=namespace,
        provider_account_id=provider_account_id,
        provider_id="perceptor",
        configuration_fingerprint=CONFIG_SHA,
        status="enabled",
        metadata={"region": "test", "profile": "fixture-v1"},
        created_at=NOW,
    )


def _adapter_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id="perceptor-adapter",
        provider_id="perceptor",
        adapter_version="1.0.0",
        contract_version="sleep-domain.v1",
        supported_data_modes={DataMode.LIVE, DataMode.REPLAY},
        supported_device_types=("millimeter_wave_radar",),
        supported_provider_account_ids=("account-1",),
        capabilities=(
            CapabilityDeclaration(
                capability=AdapterCapability.VITAL_PUSH,
                support=CapabilitySupport.YES,
                environment="test",
                verification_status=CapabilityVerificationStatus.PENDING,
            ),
        ),
        adapter_artifact_sha256=CONFIG_SHA,
        configuration_fingerprint=CONFIG_SHA,
        output_observation_schema_versions=("sleep_observation.v1",),
        deployment_status=AdapterDeploymentStatus.REGISTERED,
    )


def _binding(
    namespace: DomainNamespace,
    *,
    binding_id: str = "binding-1",
    binding_version: int = 1,
    subject_id: str = "subject-1",
    effective_from: datetime = NOW - timedelta(hours=1),
    effective_until: datetime | None = NOW + timedelta(hours=8),
) -> DeviceBinding:
    return DeviceBinding(
        data_mode=namespace.data_mode,
        device_binding_id=binding_id,
        binding_version=binding_version,
        device_id="device-1",
        provider_id="perceptor",
        provider_account_id="account-1",
        provider_device=ProviderDeviceIdentity(
            provider_device_name="imei-001"
        ),
        subject_id=subject_id,
        timezone_name="Asia/Shanghai",
        effective_from=effective_from,
        effective_until=effective_until,
        status=DeviceBindingStatus.ACTIVE,
        changed_by_actor_id="admin-1",
        change_reason="authorized fixture",
        recorded_at=NOW - timedelta(hours=1),
    )


def _raw_record(
    namespace: DomainNamespace,
    policy: RawPayloadEncryptionPolicy,
    payload: bytes,
    *,
    raw_id: str = "raw-1",
    idempotency_identity: str = "message-1",
) -> RawIngressRecord:
    return RawIngressRecord(
        raw_ingress_record_id=raw_id,
        data_mode=namespace.data_mode,
        provider_id="perceptor",
        provider_account_id="account-1",
        event_type="VitalSignsDataEvent",
        message_id=idempotency_identity,
        request_signed_at=NOW - timedelta(seconds=2),
        measurement_at=NOW,
        event_occurred_at=None,
        received_at=NOW + timedelta(seconds=1),
        signature_profile="perceptor-hmac.v1",
        signature_verification=SignatureVerificationState.VERIFIED,
        idempotency_identity=idempotency_identity,
        idempotency_version="provider-message-id.v1",
        pre_normalization_payload_sha256=hashlib.sha256(payload).hexdigest(),
        encrypted_payload_reference=f"db:sleep_domain_raw_inbox:{raw_id}",
        content_type="application/json",
        payload_size_bytes=len(payload),
        retention_deadline=(
            NOW + timedelta(seconds=1) + policy.retention_period
        ),
    )


def _intake(
    repository: SleepDomainRepository,
    namespace: DomainNamespace,
    policy: RawPayloadEncryptionPolicy,
    *,
    payload: bytes = b'{"heart_rate":68}',
    raw_id: str = "raw-1",
    work_id: str = "work-1",
    intent_id: str = "processing-intent-1",
    idempotency_identity: str = "message-1",
):
    record = _raw_record(
        namespace,
        policy,
        payload,
        raw_id=raw_id,
        idempotency_identity=idempotency_identity,
    )
    result = repository.intake_raw(
        namespace,
        record,
        raw_payload=payload,
        work_id=work_id,
        work_generation=1,
        work_json={"normalizer": "perceptor-adapter", "generation": 1},
        processing_intent_id=intent_id,
        processing_intent_json={"event_type": "RAW_ACCEPTED"},
        created_at=NOW + timedelta(seconds=1),
    )
    return record, result


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
    )


def _candidate(
    namespace: DomainNamespace,
    raw: RawIngressRecord,
) -> AdapterObservationCandidate:
    return AdapterObservationCandidate(
        candidate_id=f"candidate:{raw.raw_ingress_record_id}",
        data_mode=namespace.data_mode,
        observation_type=ObservationType.HEART_RATE,
        payload=HeartRatePayload(value=68),
        source_kind=SourceKind.DEVICE_MEASURED,
        provider_id="perceptor",
        provider_account_id="account-1",
        provider_device=ProviderDeviceIdentity(
            provider_device_name="imei-001"
        ),
        request_signed_at=raw.request_signed_at,
        measurement_at=NOW,
        event_occurred_at=None,
        received_at=raw.received_at,
        source_timestamp_text=str(int(NOW.timestamp())),
        timezone_status=TimezoneStatus.KNOWN,
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="account-1",
            adapter_id="perceptor-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
            source_record_id=raw.message_id,
            source_idempotency_key=raw.idempotency_identity,
        ),
        source_key=f"source:{raw.raw_ingress_record_id}:heart-rate",
        idempotency_key=f"fact:{raw.raw_ingress_record_id}:heart-rate",
    )


def _observation(
    namespace: DomainNamespace,
    raw: RawIngressRecord,
    binding: DeviceBinding,
) -> SleepObservation:
    return bind_adapter_candidate(
        _candidate(namespace, raw),
        binding,
        observation_id=f"observation:{raw.raw_ingress_record_id}",
    )


def _episode(
    namespace: DomainNamespace,
    *,
    episode_id: str = "night-1",
    subject_id: str = "subject-1",
) -> NightEpisode:
    return NightEpisode(
        night_episode_id=episode_id,
        data_mode=namespace.data_mode,
        subject_id=subject_id,
        timezone_name="Asia/Shanghai",
        local_sleep_date=date(2026, 7, 30),
        night_key="subject-1:2026-07-30:boundary-v1",
        collection_start_at=NOW,
        collection_end_at=None,
        collection_window_derivation=CollectionWindowDerivation.VERIFIED_IN_BED,
        binding_references=(
            DeviceBindingReference(
                device_binding_id="binding-1",
                binding_version=1,
                device_id="device-1",
            ),
        ),
        observation_ids=(),
        data_sufficiency=DataSufficiency.PARTIAL,
        pinned_adapter_versions={"perceptor-adapter": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={"night-boundary": "1.0.0"},
        state=NightEpisodeState.COLLECTING,
        created_at=NOW,
        updated_at=NOW,
    )


def _receipt(
    namespace: DomainNamespace,
    raw_id: str,
    *,
    receipt_id: str = "receipt-normalized-1",
    outcome: ProcessingOutcome = ProcessingOutcome.SUCCEEDED,
    quarantine_reason: QuarantineReason | None = None,
) -> ProcessingReceipt:
    return ProcessingReceipt(
        receipt_id=receipt_id,
        raw_ingress_record_id=raw_id,
        data_mode=namespace.data_mode,
        stage=ProcessingStage.NORMALIZATION,
        outcome=outcome,
        occurred_at=NOW + timedelta(seconds=3),
        actor_id="normalizer-worker-1",
        processor_id="perceptor-adapter",
        processor_version="1.0.0",
        quarantine_reason=quarantine_reason,
    )


def _event(
    namespace: DomainNamespace,
    *,
    event_id: str = "event-1",
    episode_id: str = "night-1",
    subject_id: str = "subject-1",
    sequence: int = 1,
) -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        event_type=DomainEventType.ELDER_IN_BED,
        event_version="1",
        data_mode=namespace.data_mode,
        aggregate_type="NightEpisode",
        aggregate_id=episode_id,
        aggregate_version=1,
        per_aggregate_sequence=sequence,
        delivery_offset=1,
        subject_id=subject_id,
        night_episode_id=episode_id,
        event_occurred_at=NOW,
        persisted_at=NOW + timedelta(seconds=3),
        correlation_id=f"correlation:{event_id}",
        attributes={"source": "canonical_observation"},
    )


def _prepare_normalization(
    repository: SleepDomainRepository,
    namespace: DomainNamespace,
    policy: RawPayloadEncryptionPolicy,
):
    repository.save_provider_account(_provider_account(namespace))
    binding = repository.append_device_binding(namespace, _binding(namespace))
    raw, _ = _intake(repository, namespace, policy)
    lease = repository.lease_normalization_work(
        namespace,
        worker_id="normalizer-worker-1",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    return raw, binding, lease


def test_canonical_baseline_contains_sleep_domain_and_removes_retired_runtime() -> None:
    assert SCHEMA_VERSION == "001_initial_schema"
    sql = POSTGRES_BASELINE_SQL
    required_tables = {
        "sleep_domain_adapters",
        "sleep_domain_provider_accounts",
        "sleep_domain_device_bindings",
        "sleep_domain_raw_inbox",
        "sleep_domain_processing_receipts",
        "sleep_domain_quarantine",
        "sleep_domain_adapter_candidates",
        "sleep_domain_canonical_observations",
        "sleep_domain_night_episodes",
        "sleep_domain_night_episode_revisions",
        "sleep_domain_analysis_revisions",
        "sleep_domain_operations",
        "sleep_domain_normalization_work",
        "sleep_domain_processing_outbox",
        "sleep_domain_domain_outbox",
    }
    for table in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE OR REPLACE FUNCTION sleep_domain_reject_raw_mutation" in sql
    assert "SKIP LOCKED" in sql
    assert "AUTOINCREMENT" not in sql
    assert "BYTEA NOT NULL" in sql
    assert "BIGSERIAL PRIMARY KEY" in sql
    assert "radar_dynamic_goals" not in sql
    assert "radar_model_invocations" not in sql

    connection = sqlite3.connect(":memory:")
    apply_sqlite_schema(connection)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert required_tables <= tables
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert {
        "sleep_domain_raw_inbox_immutable_update",
        "sleep_domain_raw_inbox_immutable_delete",
    } <= triggers
    assert len(split_sql_statements(sql)) > 600


def test_production_encryption_configuration_fails_closed() -> None:
    with pytest.raises(RawPayloadConfigurationError, match="production"):
        RawPayloadEncryptionPolicy.from_environment(
            production=True,
            environ={},
        )
    with pytest.raises(RawPayloadConfigurationError, match="retention"):
        RawPayloadEncryptionPolicy(
            key_id="key-1",
            key=Fernet.generate_key(),
            retention_period=timedelta(0),
            production=False,
        )

    key = Fernet.generate_key().decode("ascii")
    policy = RawPayloadEncryptionPolicy.from_environment(
        production=True,
        environ={
            "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY": key,
            "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY_ID": "prod-key-v1",
            "SLEEP_DOMAIN_RAW_RETENTION_SECONDS": "86400",
        },
    )
    assert policy.production is True
    assert policy.retention_period == timedelta(days=1)


def test_adapter_and_provider_accounts_are_immutable_and_secret_free() -> None:
    repository, _ = _repository()
    descriptor = _adapter_descriptor()

    assert repository.save_adapter_descriptor(
        LIVE,
        descriptor,
        created_at=NOW,
    ) == descriptor
    assert repository.save_adapter_descriptor(
        LIVE,
        descriptor,
        created_at=NOW,
    ) == descriptor
    repository.save_provider_account(_provider_account(LIVE))
    repository.save_provider_account(_provider_account(LIVE))

    changed = descriptor.model_copy(
        update={"deployment_status": AdapterDeploymentStatus.ENABLED}
    )
    with pytest.raises(ImmutableRecordConflictError):
        repository.save_adapter_descriptor(LIVE, changed, created_at=NOW)
    with pytest.raises(ValueError, match="secret fields"):
        repository.save_provider_account(
            ProviderAccountRecord(
                **{
                    **_provider_account(LIVE).__dict__,
                    "metadata": {"api_token": "must-not-store"},
                }
            )
        )


def test_intake_encrypts_raw_and_is_atomic_idempotent_and_immutable() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    repository.save_provider_account(_provider_account(LIVE))
    payload = b'{"secret_sentinel":"raw-only","heart_rate":68}'

    record, first = _intake(repository, LIVE, policy, payload=payload)

    assert first.created is True
    row = connection.execute(
        """
        SELECT encrypted_payload, encryption_key_id, raw_metadata_json,
               retention_until
        FROM sleep_domain_raw_inbox WHERE raw_ingress_record_id = ?
        """,
        (record.raw_ingress_record_id,),
    ).fetchone()
    assert row is not None
    assert bytes(row[0]) != payload
    assert b"secret_sentinel" not in bytes(row[0])
    assert row[1] == "test-key-v1"
    assert "secret_sentinel" not in str(row[2])
    assert row[3] == record.retention_deadline.isoformat()
    assert repository.load_raw_payload(
        LIVE,
        raw_ingress_record_id=record.raw_ingress_record_id,
    ) == payload
    assert repository.count_rows("sleep_domain_raw_inbox") == 1
    assert repository.count_rows("sleep_domain_normalization_work") == 1
    assert repository.count_rows("sleep_domain_processing_outbox") == 1

    _, duplicate = _intake(repository, LIVE, policy, payload=payload)
    assert duplicate.created is False
    assert duplicate == first.__class__(
        raw_ingress_record_id=first.raw_ingress_record_id,
        work_id=first.work_id,
        processing_intent_id=first.processing_intent_id,
        created=False,
    )
    assert repository.count_rows("sleep_domain_raw_inbox") == 1

    changed_payload = b'{"secret_sentinel":"collision"}'
    collision = _raw_record(
        LIVE,
        policy,
        changed_payload,
        raw_id="raw-collision",
        idempotency_identity=record.idempotency_identity,
    )
    with pytest.raises(IdempotencyConflictError):
        repository.intake_raw(
            LIVE,
            collision,
            raw_payload=changed_payload,
            work_id="work-collision",
            work_generation=1,
            work_json={"normalizer": "fixture"},
            processing_intent_id="intent-collision",
            processing_intent_json={"event_type": "RAW_ACCEPTED"},
            created_at=NOW + timedelta(seconds=1),
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            """
            UPDATE sleep_domain_raw_inbox
            SET encrypted_payload = ?
            WHERE raw_ingress_record_id = ?
            """,
            (b"tampered", record.raw_ingress_record_id),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM sleep_domain_raw_inbox WHERE raw_ingress_record_id = ?",
            (record.raw_ingress_record_id,),
        )
    connection.rollback()


def test_intake_rolls_back_all_three_records_on_last_write_failure() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    repository.save_provider_account(_provider_account(LIVE))
    connection.execute(
        """
        CREATE TRIGGER fail_processing_intent
        BEFORE INSERT ON sleep_domain_processing_outbox
        BEGIN SELECT RAISE(ABORT, 'processing intent failure'); END
        """
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="processing intent failure"):
        _intake(repository, LIVE, policy)

    assert repository.count_rows("sleep_domain_raw_inbox") == 0
    assert repository.count_rows("sleep_domain_normalization_work") == 0
    assert repository.count_rows("sleep_domain_processing_outbox") == 0


def test_raw_ciphertext_is_context_bound_and_authenticated() -> None:
    policy = _policy()
    encrypted = policy.encrypt(
        raw_ingress_record_id="raw-1",
        namespace_id=LIVE.namespace_id,
        payload=b"raw-health-payload",
        encrypted_at=NOW,
    )
    with pytest.raises(RawPayloadDecryptionError, match="context"):
        policy.decrypt(
            raw_ingress_record_id="raw-other",
            namespace_id=LIVE.namespace_id,
            ciphertext=encrypted.ciphertext,
            key_id=encrypted.key_id,
        )
    tampered = encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1])
    with pytest.raises(RawPayloadDecryptionError, match="authentication"):
        policy.decrypt(
            raw_ingress_record_id="raw-1",
            namespace_id=LIVE.namespace_id,
            ciphertext=tampered,
            key_id=encrypted.key_id,
        )


def test_device_bindings_are_append_only_non_overlapping_history() -> None:
    repository, _ = _repository()
    repository.save_provider_account(_provider_account(LIVE))
    first = _binding(
        LIVE,
        effective_from=NOW,
        effective_until=NOW + timedelta(hours=1),
    )
    second = _binding(
        LIVE,
        binding_id="binding-2",
        binding_version=2,
        subject_id="subject-2",
        effective_from=NOW + timedelta(hours=1),
        effective_until=None,
    )
    repository.append_device_binding(LIVE, first)
    repository.append_device_binding(LIVE, second)

    overlapping = _binding(
        LIVE,
        binding_id="binding-overlap",
        binding_version=3,
        subject_id="subject-3",
        effective_from=NOW + timedelta(minutes=30),
        effective_until=NOW + timedelta(hours=2),
    )
    with pytest.raises(DeviceBindingOverlapError):
        repository.append_device_binding(LIVE, overlapping)

    bindings = repository.list_device_bindings(LIVE, device_id="device-1")
    assert [(item.binding_version, item.subject_id) for item in bindings] == [
        (1, "subject-1"),
        (2, "subject-2"),
    ]
    changed_first = first.model_copy(update={"subject_id": "subject-overwritten"})
    with pytest.raises(ImmutableRecordConflictError):
        repository.append_device_binding(LIVE, changed_first)


def test_concurrent_overlapping_bindings_have_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "binding-concurrency.sqlite3"
    policy = _policy()
    first_repo, first_connection = _repository(
        sqlite3.connect(database, check_same_thread=False, timeout=5),
        policy=policy,
    )
    second_repo, second_connection = _repository(
        sqlite3.connect(database, check_same_thread=False, timeout=5),
        policy=policy,
    )
    first_repo.save_provider_account(_provider_account(LIVE))
    first = _binding(
        LIVE,
        binding_id="binding-concurrent-1",
        binding_version=1,
        effective_from=NOW,
        effective_until=NOW + timedelta(hours=2),
    )
    second = _binding(
        LIVE,
        binding_id="binding-concurrent-2",
        binding_version=2,
        effective_from=NOW + timedelta(minutes=10),
        effective_until=NOW + timedelta(hours=3),
    )

    def attempt(
        repository: SleepDomainRepository,
        binding: DeviceBinding,
    ) -> str:
        try:
            repository.append_device_binding(LIVE, binding)
            return "inserted"
        except DeviceBindingOverlapError:
            return "overlap"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda args: attempt(*args),
                ((first_repo, first), (second_repo, second)),
            )
        )

    assert sorted(outcomes) == ["inserted", "overlap"]
    assert first_repo.count_rows("sleep_domain_device_bindings") == 1
    first_connection.close()
    second_connection.close()


def test_live_and_replay_namespaces_cannot_cross_link() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    repository.save_provider_account(_provider_account(LIVE))
    repository.save_provider_account(_provider_account(REPLAY))
    live_raw, _ = _intake(repository, LIVE, policy)
    replay_raw, _ = _intake(
        repository,
        REPLAY,
        policy,
        raw_id="replay-raw-1",
        work_id="replay-work-1",
        intent_id="replay-intent-1",
    )
    repository.create_night_episode(LIVE, _episode(LIVE))
    repository.create_night_episode(
        REPLAY,
        _episode(REPLAY, episode_id="replay-night-1"),
    )

    assert live_raw.data_mode == DataMode.LIVE
    assert replay_raw.data_mode == DataMode.REPLAY
    assert repository.count_rows("sleep_domain_night_episodes") == 2
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO sleep_domain_adapter_candidates (
              candidate_id, namespace_id, data_mode, raw_ingress_record_id,
              provider_account_id, source_key, idempotency_key,
              observation_type, candidate_json, received_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cross-mode-candidate",
                LIVE.namespace_id,
                DataMode.LIVE.value,
                replay_raw.raw_ingress_record_id,
                "account-1",
                "cross-source",
                "cross-idem",
                "heart_rate",
                "{}",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO sleep_domain_provider_accounts (
              namespace_id, data_mode, provider_account_id, provider_id,
              configuration_fingerprint, status,
              account_metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "live:wrong-mode",
                "replay",
                "bad-account",
                "perceptor",
                CONFIG_SHA,
                "enabled",
                "{}",
                NOW.isoformat(),
            ),
        )
    connection.rollback()


def test_normalization_transaction_commits_fact_state_receipt_and_outbox() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    raw, binding, lease = _prepare_normalization(repository, LIVE, policy)
    candidate = _candidate(LIVE, raw)
    observation = _observation(LIVE, raw, binding)
    episode = _episode(LIVE)
    repository.create_night_episode(LIVE, episode)
    updated_episode = episode.model_copy(
        update={
            "observation_ids": (observation.observation_id,),
            "updated_at": NOW + timedelta(seconds=3),
        }
    )
    repository.append_adapter_candidate(LIVE, candidate)
    repository.append_adapter_candidate(LIVE, candidate)
    with pytest.raises(ImmutableRecordConflictError):
        repository.append_adapter_candidate(
            LIVE,
            candidate.model_copy(update={"source_key": "changed-source-key"}),
        )
    receipt = _receipt(LIVE, raw.raw_ingress_record_id)
    event = _event(LIVE)

    result = repository.commit_normalization(
        LIVE,
        work_id=lease.work_id,
        worker_id=lease.lease_owner,
        candidate=candidate,
        observation=observation,
        processing_receipt=receipt,
        domain_event=event,
        night_episode=updated_episode,
        night_episode_expected_cas_version=0,
        committed_at=NOW + timedelta(seconds=3),
    )

    assert result.created is True
    assert result.event.delivery_offset >= 1
    assert repository.count_rows("sleep_domain_adapter_candidates") == 1
    assert repository.count_rows("sleep_domain_canonical_observations") == 1
    assert repository.count_rows("sleep_domain_night_episodes") == 1
    assert repository.count_rows("sleep_domain_processing_receipts") == 1
    assert repository.count_rows("sleep_domain_domain_outbox") == 1
    assert connection.execute(
        "SELECT status FROM sleep_domain_normalization_work WHERE work_id = ?",
        (lease.work_id,),
    ).fetchone() == ("completed",)
    assert connection.execute(
        """
        SELECT cas_version FROM sleep_domain_night_episodes
        WHERE night_episode_id = ?
        """,
        (episode.night_episode_id,),
    ).fetchone() == (1,)
    assert repository.count_rows("sleep_domain_raw_inbox") == 1

    duplicate = repository.commit_normalization(
        LIVE,
        work_id=lease.work_id,
        worker_id=lease.lease_owner,
        candidate=candidate,
        observation=observation,
        processing_receipt=receipt,
        domain_event=event,
        night_episode=updated_episode,
        night_episode_expected_cas_version=0,
        committed_at=NOW + timedelta(seconds=3),
    )
    assert duplicate.created is False
    assert duplicate.event == result.event
    assert repository.count_rows("sleep_domain_canonical_observations") == 1
    assert repository.count_rows("sleep_domain_domain_outbox") == 1


def test_normalization_rolls_back_fact_and_state_when_outbox_fails() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    raw, binding, lease = _prepare_normalization(repository, LIVE, policy)
    connection.execute(
        """
        CREATE TRIGGER fail_domain_event
        BEFORE INSERT ON sleep_domain_domain_outbox
        BEGIN SELECT RAISE(ABORT, 'domain outbox failure'); END
        """
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="domain outbox failure"):
        repository.commit_normalization(
            LIVE,
            work_id=lease.work_id,
            worker_id=lease.lease_owner,
            candidate=_candidate(LIVE, raw),
            observation=_observation(LIVE, raw, binding),
            processing_receipt=_receipt(LIVE, raw.raw_ingress_record_id),
            domain_event=_event(LIVE),
            night_episode=_episode(LIVE),
            committed_at=NOW + timedelta(seconds=3),
        )

    for table in (
        "sleep_domain_adapter_candidates",
        "sleep_domain_canonical_observations",
        "sleep_domain_night_episodes",
        "sleep_domain_processing_receipts",
        "sleep_domain_domain_outbox",
    ):
        assert repository.count_rows(table) == 0
    assert connection.execute(
        "SELECT status FROM sleep_domain_normalization_work WHERE work_id = ?",
        (lease.work_id,),
    ).fetchone() == ("leased",)


def test_normalization_work_lease_is_cross_connection_and_reclaimable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work-lease.sqlite3"
    policy = _policy()
    first, first_connection = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    second, second_connection = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    first.save_provider_account(_provider_account(LIVE))
    _intake(first, LIVE, policy)

    first_lease = first.lease_normalization_work(
        LIVE,
        worker_id="worker-1",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    assert first_lease is not None
    assert second.lease_normalization_work(
        LIVE,
        worker_id="worker-2",
        now=NOW + timedelta(seconds=3),
        lease_duration=timedelta(seconds=30),
    ) is None
    recovered = second.lease_normalization_work(
        LIVE,
        worker_id="worker-2",
        now=NOW + timedelta(seconds=33),
        lease_duration=timedelta(seconds=30),
    )
    assert recovered is not None
    assert recovered.attempt_count == 2
    assert recovered.lease_owner == "worker-2"
    first_connection.close()
    second_connection.close()


def test_processing_and_domain_outboxes_have_lease_and_delivery_cas() -> None:
    policy = _policy()
    repository, _ = _repository(policy=policy)
    raw, binding, work_lease = _prepare_normalization(repository, LIVE, policy)
    processing_lease = repository.lease_processing_outbox(
        LIVE,
        worker_id="processing-publisher",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(minutes=1),
    )
    assert processing_lease is not None
    assert repository.mark_outbox_delivered(
        LIVE,
        processing_lease,
        delivered_at=NOW + timedelta(seconds=3),
    )
    assert not repository.mark_outbox_delivered(
        LIVE,
        processing_lease,
        delivered_at=NOW + timedelta(seconds=4),
    )

    repository.commit_normalization(
        LIVE,
        work_id=work_lease.work_id,
        worker_id=work_lease.lease_owner,
        candidate=_candidate(LIVE, raw),
        observation=_observation(LIVE, raw, binding),
        processing_receipt=_receipt(LIVE, raw.raw_ingress_record_id),
        domain_event=_event(LIVE),
        night_episode=_episode(LIVE),
        committed_at=NOW + timedelta(seconds=3),
    )
    domain_lease = repository.lease_domain_outbox(
        LIVE,
        worker_id="domain-publisher",
        now=NOW + timedelta(seconds=4),
        lease_duration=timedelta(minutes=1),
    )
    assert domain_lease is not None
    assert json.loads(domain_lease.payload_json)["event_id"] == "event-1"
    assert repository.mark_outbox_delivered(
        LIVE,
        domain_lease,
        delivered_at=NOW + timedelta(seconds=5),
    )


def test_append_only_revisions_and_current_pointer_use_cas() -> None:
    repository, connection = _repository()
    episode = _episode(LIVE)
    repository.create_night_episode(LIVE, episode)
    revision_1 = NightEpisodeRevision(
        night_episode_revision_id="night-revision-1",
        night_episode_id=episode.night_episode_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=("observation-1",),
        observation_set_sha256=OBSERVATION_SET_SHA,
        data_sufficiency=DataSufficiency.PARTIAL,
        created_at=NOW + timedelta(hours=8),
    )
    revision_2 = NightEpisodeRevision(
        night_episode_revision_id="night-revision-2",
        night_episode_id=episode.night_episode_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=2,
        parent_revision_id=revision_1.night_episode_revision_id,
        revision_cause=NightRevisionCause.LATE_OBSERVATION,
        observation_ids=("observation-1", "observation-2"),
        observation_set_sha256="c" * 64,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        created_at=NOW + timedelta(hours=9),
    )
    repository.append_night_episode_revision(LIVE, revision_1)
    assert repository.compare_and_set_current_night_revision(
        LIVE,
        night_episode_id=episode.night_episode_id,
        expected_cas_version=0,
        expected_current_revision_id=None,
        next_revision_id=revision_1.night_episode_revision_id,
        updated_at=revision_1.created_at,
    )
    assert not repository.compare_and_set_current_night_revision(
        LIVE,
        night_episode_id=episode.night_episode_id,
        expected_cas_version=0,
        expected_current_revision_id=None,
        next_revision_id=revision_1.night_episode_revision_id,
        updated_at=revision_1.created_at,
    )
    repository.append_night_episode_revision(LIVE, revision_2)
    assert repository.compare_and_set_current_night_revision(
        LIVE,
        night_episode_id=episode.night_episode_id,
        expected_cas_version=1,
        expected_current_revision_id=revision_1.night_episode_revision_id,
        next_revision_id=revision_2.night_episode_revision_id,
        updated_at=revision_2.created_at,
    )
    pointer = repository.get_current_night_revision(
        LIVE,
        night_episode_id=episode.night_episode_id,
    )
    assert (
        pointer.current_revision_id,
        pointer.current_revision_number,
        pointer.cas_version,
    ) == ("night-revision-2", 2, 2)
    stored_episode = json.loads(
        connection.execute(
            """
            SELECT episode_json FROM sleep_domain_night_episodes
            WHERE night_episode_id = ?
            """,
            (episode.night_episode_id,),
        ).fetchone()[0]
    )
    assert (
        stored_episode["current_night_episode_revision_id"]
        == "night-revision-2"
    )

    changed = revision_1.model_copy(
        update={"observation_set_sha256": "d" * 64}
    )
    with pytest.raises(ImmutableRecordConflictError):
        repository.append_night_episode_revision(LIVE, changed)

    analysis = AnalysisRevision(
        analysis_revision_id="analysis-revision-1",
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=revision_2.night_episode_revision_id,
        night_episode_revision_number=2,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        analysis_run_id="internal-product-episode-run-1",
        observation_set_sha256=revision_2.observation_set_sha256,
        adapter_versions={"perceptor-adapter": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"evidence": "1.0.0"},
        data_sufficiency=DataSufficiency.SUFFICIENT,
        created_at=NOW + timedelta(hours=9, minutes=1),
    )
    repository.append_analysis_revision(LIVE, analysis)
    repository.append_analysis_revision(LIVE, analysis)
    with pytest.raises(ImmutableRecordConflictError):
        repository.append_analysis_revision(
            LIVE,
            analysis.model_copy(update={"analysis_run_id": "different-run"}),
        )


def test_current_pointer_cas_has_one_cross_connection_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "night-pointer-cas.sqlite3"
    policy = _policy()
    first, first_connection = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    second, second_connection = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    episode = _episode(LIVE)
    first.create_night_episode(LIVE, episode)
    revision = NightEpisodeRevision(
        night_episode_revision_id="cas-revision-1",
        night_episode_id=episode.night_episode_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=(),
        observation_set_sha256=OBSERVATION_SET_SHA,
        data_sufficiency=DataSufficiency.PARTIAL,
        created_at=NOW + timedelta(hours=8),
    )
    first.append_night_episode_revision(LIVE, revision)

    def publish(repository: SleepDomainRepository) -> bool:
        return repository.compare_and_set_current_night_revision(
            LIVE,
            night_episode_id=episode.night_episode_id,
            expected_cas_version=0,
            expected_current_revision_id=None,
            next_revision_id=revision.night_episode_revision_id,
            updated_at=revision.created_at,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, (first, second)))

    assert sorted(outcomes) == [False, True]
    assert first.get_current_night_revision(
        LIVE,
        night_episode_id=episode.night_episode_id,
    ).cas_version == 1
    first_connection.close()
    second_connection.close()


def test_operation_idempotency_lease_and_cas() -> None:
    repository, _ = _repository()
    operation = Operation(
        operation_id="operation-1",
        data_mode=DataMode.LIVE,
        operation_type="request_reanalysis",
        subject_id="subject-1",
        service_principal_id="chatbot-1",
        actor_id="actor-1",
        target_resource_id=None,
        idempotency_key="idempotency-1",
        request_sha256=CONFIG_SHA,
        status=OperationStatus.PENDING,
        correlation_id="correlation-operation-1",
        created_at=NOW,
        updated_at=NOW,
    )
    stored, created = repository.create_operation(LIVE, operation)
    assert created and stored == operation
    duplicate, duplicate_created = repository.create_operation(
        LIVE,
        operation.model_copy(update={"operation_id": "operation-other"}),
    )
    assert not duplicate_created
    assert duplicate.operation_id == operation.operation_id

    conflict = operation.model_copy(
        update={"operation_id": "operation-conflict", "request_sha256": "e" * 64}
    )
    with pytest.raises(IdempotencyConflictError):
        repository.create_operation(LIVE, conflict)

    leased = repository.lease_operation(
        LIVE,
        operation_id=operation.operation_id,
        worker_id="operation-worker-1",
        now=NOW + timedelta(seconds=1),
        lease_duration=timedelta(minutes=1),
        expected_cas_version=0,
    )
    assert leased is not None
    assert leased.status == OperationStatus.RUNNING
    assert leased.attempt_count == 1
    assert repository.lease_operation(
        LIVE,
        operation_id=operation.operation_id,
        worker_id="operation-worker-2",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(minutes=1),
        expected_cas_version=1,
    ) is None

    with pytest.raises(ImmutableRecordConflictError, match="identity"):
        repository.compare_and_set_operation(
            LIVE,
            leased.model_copy(update={"subject_id": "different-subject"}),
            expected_cas_version=1,
        )

    succeeded = Operation.model_validate(
        {
            **leased.model_dump(mode="python"),
            "status": OperationStatus.SUCCEEDED,
            "lease_owner": None,
            "lease_expires_at": None,
            "result_resource_id": "analysis-revision-1",
            "updated_at": NOW + timedelta(seconds=3),
        }
    )
    assert repository.compare_and_set_operation(
        LIVE,
        succeeded,
        expected_cas_version=1,
    )
    assert not repository.compare_and_set_operation(
        LIVE,
        succeeded,
        expected_cas_version=1,
    )


def test_quarantine_is_append_only_receipt_state_not_raw_mutation() -> None:
    policy = _policy()
    repository, connection = _repository(policy=policy)
    repository.save_provider_account(_provider_account(LIVE))
    raw, _ = _intake(repository, LIVE, policy)
    receipt = _receipt(
        LIVE,
        raw.raw_ingress_record_id,
        receipt_id="receipt-quarantine-1",
        outcome=ProcessingOutcome.QUARANTINED,
        quarantine_reason=QuarantineReason.UNKNOWN_FORMAT,
    )
    repository.append_quarantine(
        LIVE,
        quarantine_id="quarantine-1",
        receipt=receipt,
        detail={"format": "unsupported-fixture"},
    )
    repair = ProcessingReceipt(
        receipt_id="receipt-repair-1",
        raw_ingress_record_id=raw.raw_ingress_record_id,
        data_mode=DataMode.LIVE,
        stage=ProcessingStage.REPAIR,
        outcome=ProcessingOutcome.RELEASED,
        occurred_at=NOW + timedelta(seconds=4),
        actor_id="authorized-repair-actor",
        processor_id="repair-tool",
        processor_version="1.0.0",
        cause_receipt_id=receipt.receipt_id,
    )
    deletion = ProcessingReceipt(
        receipt_id="receipt-deletion-1",
        raw_ingress_record_id=raw.raw_ingress_record_id,
        data_mode=DataMode.LIVE,
        stage=ProcessingStage.DELETION,
        outcome=ProcessingOutcome.ACCEPTED,
        occurred_at=NOW + timedelta(seconds=5),
        actor_id="retention-worker",
        processor_id="retention-policy",
        processor_version="1.0.0",
        cause_receipt_id=repair.receipt_id,
    )
    repository.append_processing_receipt(LIVE, repair)
    repository.append_processing_receipt(LIVE, deletion)

    assert repository.count_rows("sleep_domain_processing_receipts") == 3
    assert repository.count_rows("sleep_domain_quarantine") == 1
    assert repository.count_rows("sleep_domain_raw_inbox") == 1
    assert connection.execute(
        """
        SELECT stage, outcome FROM sleep_domain_processing_receipts
        ORDER BY occurred_at
        """
    ).fetchall() == [
        ("normalization", "quarantined"),
        ("repair", "released"),
        ("deletion", "accepted"),
    ]
    assert connection.execute(
        """
        SELECT reason FROM sleep_domain_quarantine
        WHERE quarantine_id = 'quarantine-1'
        """
    ).fetchone() == ("unknown_format",)
