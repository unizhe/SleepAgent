from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from sleepagent.persistence import (
    SCHEMA_VERSION,
    POSTGRES_BASELINE_SQL,
    RadarPersistenceStore,
)
from sleepagent.sleep_domain import (
    AdapterObservationCandidate,
    AdministrativeAccessPolicy,
    AdministrativeAuthorizationError,
    AdministrativeScope,
    AlgorithmVersionValue,
    AvailabilityState,
    CalibrationValue,
    CandidatePromotionService,
    CandidatePromotionStatus,
    CasConflictError,
    ConfidenceValue,
    DataMode,
    DeviceBinding,
    DeviceBindingCommand,
    DeviceBindingService,
    DeviceBindingStatus,
    DomainNamespace,
    HeartRatePayload,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProcessingOutcome,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    QuarantineReason,
    QuarantineReprocessCommand,
    RawIngressRecord,
    RawPayloadEncryptionPolicy,
    SignatureVerificationState,
    SleepDomainRepository,
    SourceKind,
    TimezoneStatus,
    namespaced_provider_device_key,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
LIVE = DomainNamespace("live:binding-promotion", DataMode.LIVE)
DEVICE = ProviderDeviceIdentity(
    provider_device_id="900719925474099312345",
    provider_device_name="000012340000",
    product_id="product:01",
    project_id="project/养老-A",
    home_id="0",
)


def _policy() -> RawPayloadEncryptionPolicy:
    return RawPayloadEncryptionPolicy(
        key_id="binding-test-key",
        key=Fernet.generate_key(),
        retention_period=timedelta(days=1),
        production=False,
    )


def _repository(
    connection: sqlite3.Connection | None = None,
    *,
    policy: RawPayloadEncryptionPolicy | None = None,
) -> tuple[SleepDomainRepository, sqlite3.Connection, RawPayloadEncryptionPolicy]:
    connection = connection or sqlite3.connect(":memory:", check_same_thread=False)
    policy = policy or _policy()
    repository = SleepDomainRepository(
        RadarPersistenceStore.connect_sqlite(connection),
        raw_payload_policy=policy,
    )
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=LIVE,
            provider_account_id="account:0001",
            provider_id="perceptor",
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"profile": "binding-test"},
            created_at=NOW,
        )
    )
    return repository, connection, policy


def _access_policy() -> AdministrativeAccessPolicy:
    return AdministrativeAccessPolicy(
        {
            "admin-1": frozenset(
                {
                    AdministrativeScope.DEVICE_BINDING_WRITE,
                    AdministrativeScope.QUARANTINE_REPROCESS,
                }
            ),
            "binding-only": frozenset(
                {AdministrativeScope.DEVICE_BINDING_WRITE}
            ),
        },
        authorization_ids={
            "admin-1": frozenset(
                {
                    "authorization-1",
                    "authorization-2",
                    "authorization-3",
                    "auth-cas-a",
                    "auth-cas-b",
                    "binding-repair-authorization",
                    "repair-authorization",
                }
            ),
            "binding-only": frozenset({"binding-only-authorization"}),
        },
    )


def _binding_command(
    *,
    command_id: str = "bind-1",
    binding_id: str = "binding-1",
    expected_version: int = 0,
    subject_id: str = "subject-1",
    effective_from: datetime = NOW - timedelta(hours=2),
    effective_until: datetime | None = None,
    requested_at: datetime = NOW,
    actor_id: str = "admin-1",
    authorization_id: str = "authorization-1",
    timezone_name: str = "Asia/Shanghai",
) -> DeviceBindingCommand:
    return DeviceBindingCommand(
        command_id=command_id,
        data_mode=DataMode.LIVE,
        device_binding_id=binding_id,
        expected_binding_version=expected_version,
        device_id="internal-device-1",
        provider_id="perceptor",
        provider_account_id="account:0001",
        provider_device=DEVICE,
        subject_id=subject_id,
        timezone_name=timezone_name,
        effective_from=effective_from,
        effective_until=effective_until,
        actor_id=actor_id,
        authorization_id=authorization_id,
        change_reason=f"test command {command_id}",
        requested_at=requested_at,
    )


def _raw(
    repository: SleepDomainRepository,
    policy: RawPayloadEncryptionPolicy,
    *,
    suffix: str,
) -> RawIngressRecord:
    payload = f'{{"sample":"{suffix}"}}'.encode()
    record = RawIngressRecord(
        raw_ingress_record_id=f"raw-{suffix}",
        data_mode=DataMode.LIVE,
        provider_id="perceptor",
        provider_account_id="account:0001",
        event_type="VitalSignsDataEvent",
        message_id=f"message-{suffix}",
        request_signed_at=NOW,
        measurement_at=None,
        event_occurred_at=None,
        received_at=NOW + timedelta(seconds=1),
        signature_profile="perceptor-hmac.v1",
        signature_verification=SignatureVerificationState.VERIFIED,
        idempotency_identity=f"message-{suffix}",
        idempotency_version="provider-message-id.v1",
        pre_normalization_payload_sha256=hashlib.sha256(payload).hexdigest(),
        encrypted_payload_reference=f"db:raw-{suffix}",
        content_type="application/json",
        payload_size_bytes=len(payload),
        retention_deadline=NOW + timedelta(days=1, seconds=1),
    )
    repository.intake_raw(
        LIVE,
        record,
        raw_payload=payload,
        work_id=f"work-{suffix}",
        work_generation=1,
        work_json={"candidate": suffix},
        processing_intent_id=f"intent-{suffix}",
        processing_intent_json={"event_type": "RAW_ACCEPTED"},
        created_at=NOW + timedelta(seconds=1),
    )
    return record


def _candidate(
    raw: RawIngressRecord,
    *,
    suffix: str,
    measurement_at: datetime | None,
    event_occurred_at: datetime | None = None,
    timezone_status: TimezoneStatus = TimezoneStatus.KNOWN,
    quality_flags: tuple[str, ...] = (),
) -> AdapterObservationCandidate:
    return AdapterObservationCandidate(
        candidate_id=f"candidate-{suffix}",
        data_mode=DataMode.LIVE,
        observation_type=ObservationType.HEART_RATE,
        payload=HeartRatePayload(value=68),
        source_kind=SourceKind.DEVICE_MEASURED,
        provider_id="perceptor",
        provider_account_id="account:0001",
        provider_device=DEVICE,
        request_signed_at=raw.request_signed_at,
        measurement_at=measurement_at,
        event_occurred_at=event_occurred_at,
        received_at=raw.received_at,
        source_timestamp_text=(
            None if measurement_at is None else measurement_at.isoformat()
        ),
        timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=MissingState.PRESENT,
            confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            calibration=CalibrationValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            quality_flags=quality_flags,
        ),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="account:0001",
            adapter_id="perceptor-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
        ),
        source_key=f"source-{suffix}",
        idempotency_key=f"fact-{suffix}",
    )


def test_binding_promotion_migration_is_additive() -> None:
    assert SCHEMA_VERSION == "001_initial_schema"
    sql = POSTGRES_BASELINE_SQL
    for table in (
        "sleep_domain_device_identities",
        "sleep_domain_device_binding_audit",
        "sleep_domain_quarantine_reprocess_audit",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_namespaced_identity_keeps_external_ids_opaque_and_validates_iana_timezone(
) -> None:
    key = namespaced_provider_device_key(
        provider_id="perceptor",
        provider_account_id="account:0001",
        provider_device=DEVICE,
    )
    other_account_key = namespaced_provider_device_key(
        provider_id="perceptor",
        provider_account_id="account:0002",
        provider_device=DEVICE,
    )
    assert key.startswith("provider-device.v1:")
    assert key != other_account_key
    assert DEVICE.provider_device_id == "900719925474099312345"
    assert DEVICE.provider_device_name == "000012340000"
    with pytest.raises(ValidationError):
        ProviderDeviceIdentity(provider_device_id=900719925474099312345)

    repository, _, _ = _repository()
    service = DeviceBindingService(repository, access_policy=_access_policy())
    with pytest.raises(ValidationError, match="IANA"):
        service.apply(
            LIVE,
            _binding_command(timezone_name="UTC+08:00"),
        )


def test_effective_intervals_are_half_open_and_gaps_quarantine() -> None:
    repository, _, policy = _repository()
    binding_service = DeviceBindingService(
        repository,
        access_policy=_access_policy(),
    )
    promotion_service = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    binding_service.apply(
        LIVE,
        _binding_command(
            effective_from=NOW - timedelta(hours=1),
            effective_until=NOW + timedelta(hours=1),
        ),
    )

    inside_raw = _raw(repository, policy, suffix="inside")
    inside = promotion_service.promote(
        LIVE,
        _candidate(
            inside_raw,
            suffix="inside",
            measurement_at=NOW,
        ),
        attempt_id="inside",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert inside.status == CandidatePromotionStatus.PROMOTED
    assert inside.observation is not None
    assert inside.observation.subject_id == "subject-1"

    boundary_raw = _raw(repository, policy, suffix="boundary")
    boundary = promotion_service.promote(
        LIVE,
        _candidate(
            boundary_raw,
            suffix="boundary",
            measurement_at=NOW + timedelta(hours=1),
        ),
        attempt_id="boundary",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(hours=1, seconds=1),
    )
    assert boundary.status == CandidatePromotionStatus.QUARANTINED
    assert boundary.quarantine_reason == QuarantineReason.BINDING_TIME_OUTSIDE_INTERVAL


def test_unknown_or_untrusted_time_never_falls_back_to_receipt_or_signed_time() -> None:
    repository, _, policy = _repository()
    DeviceBindingService(repository, access_policy=_access_policy()).apply(
        LIVE,
        _binding_command(),
    )
    service = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    missing_raw = _raw(repository, policy, suffix="missing-time")
    missing = service.promote(
        LIVE,
        _candidate(
            missing_raw,
            suffix="missing-time",
            measurement_at=None,
            event_occurred_at=None,
        ),
        attempt_id="missing-time",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert missing.quarantine_reason == QuarantineReason.UNPARSEABLE_TIME

    unknown_raw = _raw(repository, policy, suffix="unknown-zone")
    unknown = service.promote(
        LIVE,
        _candidate(
            unknown_raw,
            suffix="unknown-zone",
            measurement_at=NOW,
            timezone_status=TimezoneStatus.TIMEZONE_UNKNOWN,
        ),
        attempt_id="unknown-zone",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(seconds=3),
    )
    assert unknown.quarantine_reason == QuarantineReason.TIMEZONE_UNKNOWN

    skew_raw = _raw(repository, policy, suffix="clock-skew")
    skew = service.promote(
        LIVE,
        _candidate(
            skew_raw,
            suffix="clock-skew",
            measurement_at=NOW,
            quality_flags=("clock-skew",),
        ),
        attempt_id="clock-skew",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(seconds=4),
    )
    assert skew.quarantine_reason == QuarantineReason.CLOCK_SKEW
    assert repository.count_rows("sleep_domain_canonical_observations") == 0


def test_iana_timezone_binding_handles_both_dst_fold_instants() -> None:
    repository, _, policy = _repository()
    binding_service = DeviceBindingService(
        repository,
        access_policy=_access_policy(),
    )
    promotion_service = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    binding_service.apply(
        LIVE,
        _binding_command(
            timezone_name="America/New_York",
            effective_from=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            effective_until=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        ),
    )
    local_zone = ZoneInfo("America/New_York")
    first_fold = datetime(2026, 11, 1, 1, 30, tzinfo=local_zone, fold=0)
    second_fold = datetime(2026, 11, 1, 1, 30, tzinfo=local_zone, fold=1)
    assert first_fold.astimezone(UTC) != second_fold.astimezone(UTC)

    observations = []
    for suffix, measured_at in (
        ("dst-first", first_fold),
        ("dst-second", second_fold),
    ):
        raw = _raw(repository, policy, suffix=suffix)
        result = promotion_service.promote(
            LIVE,
            _candidate(raw, suffix=suffix, measurement_at=measured_at),
            attempt_id=suffix,
            actor_id="normalizer",
            occurred_at=datetime(2026, 11, 1, 10, 1, tzinfo=UTC),
        )
        assert result.observation is not None
        observations.append(result.observation)
    assert (
        observations[0].measurement_at.astimezone(UTC)
        != observations[1].measurement_at.astimezone(UTC)
    )
    assert {item.subject_id for item in observations} == {"subject-1"}


def test_prospective_rebinding_preserves_old_observation_and_uses_event_time() -> None:
    repository, connection, policy = _repository()
    bindings = DeviceBindingService(repository, access_policy=_access_policy())
    promotions = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    bindings.apply(LIVE, _binding_command())
    old_raw = _raw(repository, policy, suffix="before-rebind")
    old_result = promotions.promote(
        LIVE,
        _candidate(
            old_raw,
            suffix="before-rebind",
            measurement_at=NOW - timedelta(minutes=1),
        ),
        attempt_id="before-rebind",
        actor_id="normalizer",
        occurred_at=NOW,
    )
    assert old_result.observation is not None
    old_json = connection.execute(
        "SELECT observation_json FROM sleep_domain_canonical_observations "
        "WHERE observation_id = ?",
        (old_result.observation.observation_id,),
    ).fetchone()[0]

    rebind_command = _binding_command(
        command_id="rebind-2",
        binding_id="binding-2",
        expected_version=1,
        subject_id="subject-2",
        effective_from=NOW + timedelta(hours=1),
        requested_at=NOW,
        authorization_id="authorization-2",
    )
    bindings.apply(LIVE, rebind_command)
    assert bindings.apply(LIVE, rebind_command).binding_version == 2
    old_history, new_history = repository.list_device_bindings(
        LIVE,
        device_id="internal-device-1",
    )
    assert old_history.effective_until == NOW + timedelta(hours=1)
    assert old_history.status == DeviceBindingStatus.ENDED
    assert new_history.binding_version == 2

    after_raw = _raw(repository, policy, suffix="after-rebind")
    after = promotions.promote(
        LIVE,
        _candidate(
            after_raw,
            suffix="after-rebind",
            measurement_at=NOW + timedelta(hours=1),
        ),
        attempt_id="after-rebind",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(hours=1, seconds=1),
    )
    assert after.observation is not None
    assert after.observation.subject_id == "subject-2"
    assert after.observation.binding_version == 2
    assert connection.execute(
        "SELECT observation_json FROM sleep_domain_canonical_observations "
        "WHERE observation_id = ?",
        (old_result.observation.observation_id,),
    ).fetchone()[0] == old_json

    with pytest.raises(ValueError, match="prospective"):
        bindings.apply(
            LIVE,
            _binding_command(
                command_id="illegal-backdate",
                binding_id="binding-3",
                expected_version=2,
                subject_id="subject-3",
                effective_from=NOW - timedelta(minutes=1),
                requested_at=NOW + timedelta(seconds=1),
                authorization_id="authorization-3",
            ),
        )


def test_same_device_cannot_gain_a_competing_active_subject() -> None:
    repository, _, _ = _repository()
    service = DeviceBindingService(repository, access_policy=_access_policy())
    service.apply(LIVE, _binding_command())
    with pytest.raises(CasConflictError):
        service.apply(
            LIVE,
            _binding_command(
                command_id="competing-initial-binding",
                binding_id="competing-binding",
                expected_version=0,
                subject_id="competing-subject",
                authorization_id="authorization-2",
            ),
        )
    bindings = repository.list_device_bindings(
        LIVE,
        device_id="internal-device-1",
    )
    assert [(item.binding_version, item.subject_id) for item in bindings] == [
        (1, "subject-1")
    ]


def test_concurrent_rebinding_uses_cas_and_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "binding-cas.sqlite3"
    policy = _policy()
    first_repo, first_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False, timeout=5),
        policy=policy,
    )
    second_repo, second_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False, timeout=5),
        policy=policy,
    )
    DeviceBindingService(first_repo, access_policy=_access_policy()).apply(
        LIVE,
        _binding_command(),
    )
    commands = (
        _binding_command(
            command_id="cas-a",
            binding_id="binding-cas-a",
            expected_version=1,
            subject_id="subject-a",
            effective_from=NOW + timedelta(hours=1),
            authorization_id="auth-cas-a",
        ),
        _binding_command(
            command_id="cas-b",
            binding_id="binding-cas-b",
            expected_version=1,
            subject_id="subject-b",
            effective_from=NOW + timedelta(hours=1),
            authorization_id="auth-cas-b",
        ),
    )

    def attempt(
        repository: SleepDomainRepository,
        command: DeviceBindingCommand,
    ) -> str:
        try:
            DeviceBindingService(
                repository,
                access_policy=_access_policy(),
            ).apply(LIVE, command)
            return "committed"
        except CasConflictError:
            return "cas_conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda args: attempt(*args),
                ((first_repo, commands[0]), (second_repo, commands[1])),
            )
        )
    assert sorted(outcomes) == ["cas_conflict", "committed"]
    assert first_repo.count_rows("sleep_domain_device_bindings") == 2
    assert first_repo.count_rows("sleep_domain_device_binding_audit") == 2
    first_connection.close()
    second_connection.close()


def test_overlapping_legacy_binding_is_ambiguous_and_never_guessed() -> None:
    repository, connection, policy = _repository()
    DeviceBindingService(repository, access_policy=_access_policy()).apply(
        LIVE,
        _binding_command(),
    )
    overlapping = DeviceBinding(
        data_mode=DataMode.LIVE,
        device_binding_id="legacy-overlap",
        binding_version=99,
        device_id="internal-device-1",
        provider_id="perceptor",
        provider_account_id="account:0001",
        provider_device=DEVICE,
        subject_id="legacy-subject",
        timezone_name="Asia/Shanghai",
        effective_from=NOW - timedelta(hours=1),
        effective_until=NOW + timedelta(hours=1),
        status=DeviceBindingStatus.ACTIVE,
        changed_by_actor_id="legacy-import",
        change_reason="corrupt legacy overlap fixture",
        recorded_at=NOW,
    )
    connection.execute(
        """
        INSERT INTO sleep_domain_device_bindings (
          device_binding_id, namespace_id, data_mode, binding_version,
          device_id, provider_id, provider_account_id, subject_id,
          timezone_name, effective_from, effective_until, status,
          binding_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            overlapping.device_binding_id,
            LIVE.namespace_id,
            LIVE.data_mode.value,
            overlapping.binding_version,
            overlapping.device_id,
            overlapping.provider_id,
            overlapping.provider_account_id,
            overlapping.subject_id,
            overlapping.timezone_name,
            overlapping.effective_from.isoformat(),
            overlapping.effective_until.isoformat(),
            overlapping.status.value,
            overlapping.model_dump_json(),
            overlapping.recorded_at.isoformat(),
        ),
    )
    connection.commit()
    raw = _raw(repository, policy, suffix="ambiguous")
    service = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    result = service.promote(
        LIVE,
        _candidate(raw, suffix="ambiguous", measurement_at=NOW),
        attempt_id="ambiguous",
        actor_id="normalizer",
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert result.status == CandidatePromotionStatus.QUARANTINED
    assert result.quarantine_reason == QuarantineReason.DEVICE_BINDING_AMBIGUOUS
    assert repository.count_rows("sleep_domain_canonical_observations") == 0
    raw_before = repository.load_raw_payload(
        LIVE,
        raw_ingress_record_id=raw.raw_ingress_record_id,
    )
    retried = service.reprocess_quarantine(
        LIVE,
        QuarantineReprocessCommand(
            request_id="ambiguous-retry",
            quarantine_ids=(result.quarantine_id,),
            actor_id="admin-1",
            authorization_id="repair-authorization",
            requested_at=NOW + timedelta(minutes=1),
            reason="verify ambiguity remains fail-closed",
        ),
    )
    assert retried.released_count == 0
    assert (
        retried.results[0].quarantine_reason
        == QuarantineReason.DEVICE_BINDING_AMBIGUOUS
    )
    assert repository.count_rows("sleep_domain_quarantine") == 2
    assert repository.count_rows("sleep_domain_canonical_observations") == 0
    assert repository.load_raw_payload(
        LIVE,
        raw_ingress_record_id=raw.raw_ingress_record_id,
    ) == raw_before


def test_authorized_reprocessing_releases_only_unique_binding_and_keeps_raw_immutable(
) -> None:
    repository, connection, policy = _repository()
    promotions = CandidatePromotionService(
        repository,
        access_policy=_access_policy(),
    )
    raw = _raw(repository, policy, suffix="repair")
    candidate = _candidate(
        raw,
        suffix="repair",
        measurement_at=NOW - timedelta(hours=1),
    )
    quarantined = promotions.promote(
        LIVE,
        candidate,
        attempt_id="repair-initial",
        actor_id="normalizer",
        occurred_at=NOW,
    )
    assert quarantined.quarantine_reason == QuarantineReason.DEVICE_UNBOUND
    raw_before = connection.execute(
        "SELECT encrypted_payload, raw_metadata_json FROM sleep_domain_raw_inbox "
        "WHERE raw_ingress_record_id = ?",
        (raw.raw_ingress_record_id,),
    ).fetchone()

    with pytest.raises(AdministrativeAuthorizationError):
        promotions.reprocess_quarantine(
            LIVE,
            QuarantineReprocessCommand(
                request_id="unauthorized-repair",
                quarantine_ids=(quarantined.quarantine_id,),
                actor_id="viewer",
                authorization_id="viewer-authorization",
                requested_at=NOW + timedelta(minutes=1),
                reason="viewer must not release data",
            ),
        )
    assert repository.count_rows("sleep_domain_quarantine_reprocess_audit") == 0

    DeviceBindingService(repository, access_policy=_access_policy()).apply(
        LIVE,
        _binding_command(
            command_id="explicit-backdated-initial-binding",
            effective_from=NOW - timedelta(hours=2),
            requested_at=NOW + timedelta(minutes=1),
            authorization_id="binding-repair-authorization",
        ),
    )
    repaired = promotions.reprocess_quarantine(
        LIVE,
        QuarantineReprocessCommand(
            request_id="authorized-repair",
            quarantine_ids=(quarantined.quarantine_id,),
            actor_id="admin-1",
            authorization_id="repair-authorization",
            requested_at=NOW + timedelta(minutes=2),
            reason="explicit binding now uniquely covers the measurement",
        ),
    )
    assert repaired.released_count == 1
    assert repaired.results[0].receipt.outcome == ProcessingOutcome.RELEASED
    assert (
        repaired.results[0].receipt.cause_receipt_id
        == quarantined.receipt.receipt_id
    )
    assert repaired.results[0].observation is not None
    assert repaired.results[0].observation.subject_id == "subject-1"
    assert repository.count_rows("sleep_domain_quarantine") == 1
    assert repository.count_rows("sleep_domain_quarantine_reprocess_audit") == 1
    assert connection.execute(
        "SELECT encrypted_payload, raw_metadata_json FROM sleep_domain_raw_inbox "
        "WHERE raw_ingress_record_id = ?",
        (raw.raw_ingress_record_id,),
    ).fetchone() == raw_before
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE sleep_domain_raw_inbox SET event_type = 'edited' "
            "WHERE raw_ingress_record_id = ?",
            (raw.raw_ingress_record_id,),
        )
