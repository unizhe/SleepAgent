from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from sleepagent.radar_agent.persistence import (
    MIGRATION_VERSION,
    RADAR_AGENT_POSTGRES_MIGRATIONS,
    RadarPersistenceStore,
)
from sleepagent.sleep_domain import (
    AdapterObservationCandidate,
    AdministrativeAccessPolicy,
    AdministrativeScope,
    AlgorithmVersionValue,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    CandidatePromotionService,
    ConfidenceValue,
    DataMode,
    DataSufficiency,
    DeviceBindingCommand,
    DeviceBindingService,
    DomainNamespace,
    EpisodeAggregationStatus,
    EpisodeBoundaryPolicy,
    EpisodeVersionPins,
    HeartRatePayload,
    InvalidLifecycleTransitionError,
    LifecycleAccessPolicy,
    LifecycleAuthorizationError,
    LifecycleTransitionPolicy,
    LifecycleTrigger,
    LifecycleTriggerKind,
    LifecycleTriggerSource,
    MissingState,
    NightEpisodeService,
    NightEpisodeState,
    NightRevisionCause,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    RawIngressRecord,
    RawPayloadEncryptionPolicy,
    SignatureVerificationState,
    SleepDomainRepository,
    SourceKind,
    TimezoneStatus,
    namespaced_provider_device_key,
)


UTC = timezone.utc
NAMESPACE = DomainNamespace("live:night-episode-tests", DataMode.LIVE)
DEVICE = ProviderDeviceIdentity(
    provider_device_id="900719925474099312345",
    provider_device_name="elder-room-radar-01",
)
CONFIG_SHA = "a" * 64


def _raw_policy(key: bytes | None = None) -> RawPayloadEncryptionPolicy:
    return RawPayloadEncryptionPolicy(
        key_id="night-episode-test-key",
        key=key or Fernet.generate_key(),
        retention_period=timedelta(days=30),
        production=False,
    )


def _repository(
    connection: sqlite3.Connection | None = None,
    *,
    policy: RawPayloadEncryptionPolicy | None = None,
) -> tuple[SleepDomainRepository, sqlite3.Connection, RawPayloadEncryptionPolicy]:
    connection = connection or sqlite3.connect(":memory:", check_same_thread=False)
    policy = policy or _raw_policy()
    repository = SleepDomainRepository(
        RadarPersistenceStore.connect_sqlite(connection),
        raw_payload_policy=policy,
    )
    if repository.get_provider_account(
        NAMESPACE,
        provider_account_id="perceptor-account",
    ) is None:
        repository.save_provider_account(
            ProviderAccountRecord(
                namespace=NAMESPACE,
                provider_account_id="perceptor-account",
                provider_id="perceptor",
                configuration_fingerprint=CONFIG_SHA,
                status="enabled",
                metadata={"fixture": "night-episode"},
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
    return repository, connection, policy


def _admin_policy() -> AdministrativeAccessPolicy:
    return AdministrativeAccessPolicy(
        {"admin": frozenset({AdministrativeScope.DEVICE_BINDING_WRITE})},
        authorization_ids={"admin": frozenset({"binding-auth"})},
    )


def _bind(
    repository: SleepDomainRepository,
    *,
    timezone_name: str,
    effective_from: datetime,
    effective_until: datetime | None = None,
    binding_id: str = "binding-v1",
) -> None:
    DeviceBindingService(
        repository,
        access_policy=_admin_policy(),
    ).apply(
        NAMESPACE,
        DeviceBindingCommand(
            command_id=f"command:{binding_id}",
            data_mode=DataMode.LIVE,
            device_binding_id=binding_id,
            expected_binding_version=0,
            device_id="internal-device-1",
            provider_id="perceptor",
            provider_account_id="perceptor-account",
            provider_device=DEVICE,
            subject_id="elder-1",
            timezone_name=timezone_name,
            effective_from=effective_from,
            effective_until=effective_until,
            actor_id="admin",
            authorization_id="binding-auth",
            change_reason="night episode test binding",
            requested_at=effective_from,
        ),
    )


def _raw(
    repository: SleepDomainRepository,
    *,
    suffix: str,
    received_at: datetime,
) -> RawIngressRecord:
    payload = f'{{"fixture":"{suffix}"}}'.encode()
    record = RawIngressRecord(
        raw_ingress_record_id=f"raw:{suffix}",
        data_mode=DataMode.LIVE,
        provider_id="perceptor",
        provider_account_id="perceptor-account",
        event_type="fixture",
        message_id=f"message:{suffix}",
        received_at=received_at,
        signature_profile="fixture.v1",
        signature_verification=SignatureVerificationState.VERIFIED,
        idempotency_identity=f"message:{suffix}",
        idempotency_version="fixture.v1",
        pre_normalization_payload_sha256=hashlib.sha256(payload).hexdigest(),
        encrypted_payload_reference=f"db:raw:{suffix}",
        content_type="application/json",
        payload_size_bytes=len(payload),
        retention_deadline=received_at + timedelta(days=30),
    )
    repository.intake_raw(
        NAMESPACE,
        record,
        raw_payload=payload,
        work_id=f"work:{suffix}",
        work_generation=1,
        work_json={"fixture": suffix},
        processing_intent_id=f"intent:{suffix}",
        processing_intent_json={"fixture": suffix},
        created_at=received_at,
    )
    return record


def _observation(
    repository: SleepDomainRepository,
    *,
    suffix: str,
    event_at: datetime,
    received_at: datetime | None = None,
    bed_state: BedPresenceState | None = None,
    adapter_version: str = "1.0.0",
) -> str:
    received_at = received_at or event_at + timedelta(seconds=1)
    raw = _raw(repository, suffix=suffix, received_at=received_at)
    observation_type = (
        ObservationType.BED_PRESENCE
        if bed_state is not None
        else ObservationType.HEART_RATE
    )
    payload = (
        BedPresencePayload(state=bed_state)
        if bed_state is not None
        else HeartRatePayload(value=65)
    )
    candidate = AdapterObservationCandidate(
        candidate_id=f"candidate:{suffix}",
        data_mode=DataMode.LIVE,
        observation_type=observation_type,
        payload=payload,
        source_kind=SourceKind.DEVICE_MEASURED,
        provider_id="perceptor",
        provider_account_id="perceptor-account",
        provider_device=DEVICE,
        measurement_at=event_at,
        event_occurred_at=event_at,
        received_at=received_at,
        source_timestamp_text=event_at.isoformat(),
        timezone_status=TimezoneStatus.KNOWN,
        quality=ObservationQuality(
            missing_state=MissingState.PRESENT,
            confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            calibration=CalibrationValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
        ),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="perceptor-account",
            adapter_id="perceptor-adapter",
            adapter_version=adapter_version,
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
        ),
        source_key=f"source:{suffix}",
        idempotency_key=f"observation:{suffix}",
    )
    promoted = CandidatePromotionService(
        repository,
        access_policy=_admin_policy(),
    ).promote(
        NAMESPACE,
        candidate,
        attempt_id=suffix,
        actor_id="normalizer",
        occurred_at=received_at,
    )
    assert promoted.observation is not None
    return promoted.observation.observation_id


def _service(
    repository: SleepDomainRepository,
    *,
    active_boundary_version: str = "boundary-v1",
    adapter_version: str = "1.0.0",
    transition_version: str = "transition-v1",
    minimum_dwell: int = 60,
    override_seconds: int = 3600,
) -> NightEpisodeService:
    policies = {
        "boundary-v1": EpisodeBoundaryPolicy(
            policy_version="boundary-v1",
            rollover_local_minute=12 * 60,
            report_deadline_local_minute=10 * 60,
            maximum_episode_seconds=20 * 3600,
            allowed_lateness_seconds=2 * 3600,
        ),
        "boundary-v2": EpisodeBoundaryPolicy(
            policy_version="boundary-v2",
            rollover_local_minute=13 * 60,
            report_deadline_local_minute=11 * 60,
            maximum_episode_seconds=20 * 3600,
            allowed_lateness_seconds=3 * 3600,
        ),
    }
    transition_policies = {
        "transition-v1": LifecycleTransitionPolicy(
            policy_version="transition-v1",
            debounce_seconds=30,
            minimum_active_dwell_seconds=minimum_dwell,
            minimum_dormant_dwell_seconds=minimum_dwell,
            minimum_collecting_dwell_seconds=minimum_dwell,
            manual_override_seconds=override_seconds,
            lease_seconds=5,
            fallback_start_local_minute=22 * 60,
            fallback_end_local_minute=8 * 60,
            fallback_tolerance_seconds=3600,
        ),
        "transition-v2": LifecycleTransitionPolicy(
            policy_version="transition-v2",
            debounce_seconds=5,
            minimum_active_dwell_seconds=0,
            minimum_dormant_dwell_seconds=0,
            minimum_collecting_dwell_seconds=0,
            manual_override_seconds=60,
            lease_seconds=5,
            fallback_start_local_minute=20 * 60,
            fallback_end_local_minute=10 * 60,
            fallback_tolerance_seconds=0,
        ),
    }
    return NightEpisodeService(
        repository,
        boundary_policies=policies,
        active_boundary_policy_version=active_boundary_version,
        transition_policy=transition_policies[transition_version],
        transition_policies=transition_policies,
        access_policy=LifecycleAccessPolicy(
            {"operator": frozenset({"lifecycle-auth"})}
        ),
        default_version_pins=EpisodeVersionPins(
            adapter_versions={"perceptor-adapter": adapter_version},
            observation_schema_versions=("sleep_observation.v1",),
            policy_versions={
                "quality": "quality-v1",
                "risk": "risk-v1",
                "fast_path_event": "fast-path-event-v1",
            },
        ),
        worker_id=f"worker:{active_boundary_version}:{adapter_version}",
    )


def _command(
    *,
    trigger_id: str,
    kind: LifecycleTriggerKind,
    occurred_at: datetime,
) -> LifecycleTrigger:
    return LifecycleTrigger(
        trigger_id=trigger_id,
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
        kind=kind,
        occurred_at=occurred_at,
        received_at=occurred_at,
        actor_id="operator",
        authorization_id="lifecycle-auth",
        correlation_id=f"correlation:{trigger_id}",
    )


def _register_report(
    repository: SleepDomainRepository,
    *,
    suffix: str,
    local_report_date: date,
    fetched_at: datetime,
    content: bytes,
) -> str:
    raw = _raw(repository, suffix=f"report:{suffix}", received_at=fetched_at)
    report_id = f"source-report:{suffix}"
    repository.register_source_report_version(
        NAMESPACE,
        source_report_version_id=report_id,
        provider_id="perceptor",
        provider_account_id="perceptor-account",
        provider_device_key=namespaced_provider_device_key(
            provider_id="perceptor",
            provider_account_id="perceptor-account",
            provider_device=DEVICE,
        ),
        local_report_date=local_report_date,
        content_sha256=hashlib.sha256(content).hexdigest(),
        raw_ingress_record_id=raw.raw_ingress_record_id,
        is_empty=False,
        fetched_at=fetched_at,
    )
    return report_id


def test_cross_midnight_event_time_membership_and_dst_night_key() -> None:
    repository, connection, _ = _repository()
    shanghai = ZoneInfo("Asia/Shanghai")
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=datetime(2026, 10, 1, tzinfo=shanghai),
    )
    service = _service(repository)
    start = datetime(2026, 10, 31, 23, 30, tzinfo=shanghai)
    end = datetime(2026, 11, 1, 7, 0, tzinfo=shanghai)
    start_id = _observation(
        repository,
        suffix="cross-midnight-start",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
    )
    opened = service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=start + timedelta(seconds=2),
    )
    assert opened.episode is not None
    assert opened.episode.local_sleep_date == date(2026, 10, 31)
    assert opened.episode.night_key == "elder-1:2026-10-31:boundary-v1"

    vital_id = _observation(
        repository,
        suffix="cross-midnight-vital",
        event_at=datetime(2026, 11, 1, 2, 30, tzinfo=shanghai),
        received_at=datetime(2026, 11, 1, 9, 30, tzinfo=shanghai),
    )
    associated = service.associate_observation(
        NAMESPACE,
        observation_id=vital_id,
        associated_at=datetime(2026, 11, 1, 9, 31, tzinfo=shanghai),
    )
    assert associated.episode is not None
    assert vital_id in associated.episode.observation_ids

    end_id = _observation(
        repository,
        suffix="cross-midnight-end",
        event_at=end,
        bed_state=BedPresenceState.OUT_OF_BED,
    )
    awaiting = service.process_observation(
        NAMESPACE,
        observation_id=end_id,
        processed_at=end + timedelta(seconds=2),
    )
    assert awaiting.episode is not None
    assert awaiting.episode.state == NightEpisodeState.AWAITING_REPORT
    assert awaiting.episode.allowed_lateness_watermark_at == end + timedelta(
        hours=2
    )
    assert repository.count_rows("sleep_domain_domain_outbox") == 4
    assert repository.count_rows(
        "sleep_domain_lifecycle_transition_receipts"
    ) == 4

    boundary = EpisodeBoundaryPolicy(policy_version="dst-policy")
    new_york = ZoneInfo("America/New_York")
    first_fold = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=new_york)
    second_fold = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=new_york)
    assert first_fold.utcoffset() != second_fold.utcoffset()
    assert boundary.derive_night_key(
        subject_id="elder-1",
        event_at=first_fold,
        timezone_name="America/New_York",
    ) == boundary.derive_night_key(
        subject_id="elder-1",
        event_at=second_fold,
        timezone_name="America/New_York",
    )
    spring_deadline, spring_flag = NightEpisodeService._report_deadline(
        date(2026, 3, 7),
        "America/New_York",
        EpisodeBoundaryPolicy(
            policy_version="spring-gap-policy",
            report_deadline_local_minute=2 * 60 + 30,
        ),
    )
    assert spring_deadline.astimezone(new_york).hour == 3
    assert spring_flag == "dst_nonexistent_deadline_shifted_forward"
    connection.close()


def test_lifecycle_migration_is_additive_and_has_no_agent_or_medical_logic() -> None:
    assert MIGRATION_VERSION == "020_legacy_authority_cutover"
    sql = RADAR_AGENT_POSTGRES_MIGRATIONS["015_night_episode_lifecycle"]
    for table in (
        "sleep_domain_monitoring_snapshots",
        "sleep_domain_subject_lifecycle_leases",
        "sleep_domain_lifecycle_transition_receipts",
        "sleep_domain_episode_observation_memberships",
        "sleep_domain_episode_source_reports",
        "sleep_domain_pending_episode_associations",
        "sleep_domain_revision_publications",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "ON DELETE CASCADE" not in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
    assert "analysis_run_id" not in sql.lower()
    assert "model_version" not in sql.lower()
    assert "heart_rate" not in sql.lower()
    assert "respiratory" not in sql.lower()


def test_pending_association_never_attaches_to_latest_night() -> None:
    repository, _, _ = _repository()
    zone = ZoneInfo("Asia/Shanghai")
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=datetime(2026, 7, 1, tzinfo=zone),
    )
    service = _service(repository)
    orphan_id = _observation(
        repository,
        suffix="orphan-vital",
        event_at=datetime(2026, 7, 30, 23, 0, tzinfo=zone),
    )
    pending = service.associate_observation(
        NAMESPACE,
        observation_id=orphan_id,
        associated_at=datetime(2026, 7, 30, 23, 1, tzinfo=zone),
    )
    assert pending.status == EpisodeAggregationStatus.PENDING_ASSOCIATION
    assert pending.pending_association is not None
    assert pending.pending_association.candidate_night_episode_ids == ()
    assert repository.count_rows(
        "sleep_domain_episode_observation_memberships"
    ) == 0


def test_priority_debounce_dwell_manual_override_and_duplicate_trigger() -> None:
    repository, _, _ = _repository()
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 7, 30, 22, 0, tzinfo=zone)
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    service = _service(repository, minimum_dwell=300, override_seconds=900)
    start_id = _observation(
        repository,
        suffix="dwell-start",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
    )
    opened = service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=start + timedelta(seconds=1),
    )
    assert opened.monitoring is not None
    assert opened.monitoring.state.value == "active"

    noisy_id = _observation(
        repository,
        suffix="dwell-noisy-end",
        event_at=start + timedelta(seconds=20),
        bed_state=BedPresenceState.OUT_OF_BED,
    )
    noisy = service.process_observation(
        NAMESPACE,
        observation_id=noisy_id,
        processed_at=start + timedelta(seconds=21),
    )
    assert noisy.status in {
        EpisodeAggregationStatus.ASSOCIATED,
        EpisodeAggregationStatus.IDEMPOTENT,
    }
    assert repository.get_night_episode(
        NAMESPACE,
        night_episode_id=opened.episode.night_episode_id,
    ).state == NightEpisodeState.COLLECTING

    manual_stop = _command(
        trigger_id="manual-stop",
        kind=LifecycleTriggerKind.DEACTIVATE,
        occurred_at=start + timedelta(minutes=1),
    )
    stopped = service.process_trigger(NAMESPACE, trigger=manual_stop)
    assert stopped.episode is not None
    assert stopped.episode.state == NightEpisodeState.AWAITING_REPORT
    events_after_stop = repository.count_rows("sleep_domain_domain_outbox")
    duplicate = service.process_trigger(NAMESPACE, trigger=manual_stop)
    assert duplicate.status == EpisodeAggregationStatus.IDEMPOTENT
    assert repository.count_rows("sleep_domain_domain_outbox") == events_after_stop

    restart_id = _observation(
        repository,
        suffix="override-in-bed",
        event_at=start + timedelta(minutes=2),
        bed_state=BedPresenceState.IN_BED,
    )
    suppressed = service.process_observation(
        NAMESPACE,
        observation_id=restart_id,
        processed_at=start + timedelta(minutes=2, seconds=1),
    )
    assert suppressed.status == EpisodeAggregationStatus.PENDING_ASSOCIATION
    assert suppressed.reason_code == "manual_override_active"

    next_night = start + timedelta(days=1, minutes=20)
    next_id = _observation(
        repository,
        suffix="override-expired-next-night",
        event_at=next_night,
        bed_state=BedPresenceState.IN_BED,
    )
    reopened = service.process_observation(
        NAMESPACE,
        observation_id=next_id,
        processed_at=next_night + timedelta(seconds=1),
    )
    assert reopened.status == EpisodeAggregationStatus.OPENED
    assert reopened.episode.night_episode_id != stopped.episode.night_episode_id


def test_report_deadline_then_late_report_appends_revision() -> None:
    repository, _, _ = _repository()
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 7, 30, 22, 0, tzinfo=zone)
    end = datetime(2026, 7, 31, 7, 0, tzinfo=zone)
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    service = _service(repository, minimum_dwell=0)
    start_id = _observation(
        repository,
        suffix="deadline-start",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
    )
    opened = service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=start + timedelta(seconds=1),
    )
    end_id = _observation(
        repository,
        suffix="deadline-end",
        event_at=end,
        bed_state=BedPresenceState.OUT_OF_BED,
    )
    awaiting = service.process_observation(
        NAMESPACE,
        observation_id=end_id,
        processed_at=end + timedelta(seconds=1),
    )
    deadline = awaiting.episode.report_deadline_at
    first = service.publish_report_deadline(
        NAMESPACE,
        night_episode_id=opened.episode.night_episode_id,
        trigger_id="morning-deadline",
        published_at=deadline,
    )
    assert first.revision is not None
    assert first.revision.revision_number == 1
    assert first.revision.parent_revision_id is None
    assert first.revision.revision_cause == NightRevisionCause.REPORT_DEADLINE
    assert first.revision.data_sufficiency == DataSufficiency.REPORT_PENDING
    assert "vendor_report_pending" in first.revision.quality_flags

    report_id = _register_report(
        repository,
        suffix="late-vendor-report",
        local_report_date=date(2026, 7, 30),
        fetched_at=deadline + timedelta(hours=2),
        content=b'{"sleep":"complete"}',
    )
    revised = service.associate_source_report(
        NAMESPACE,
        source_report_version_id=report_id,
        associated_at=deadline + timedelta(hours=2, minutes=1),
    )
    assert revised.revision is not None
    assert revised.revision.revision_number == 2
    assert (
        revised.revision.parent_revision_id
        == first.revision.night_episode_revision_id
    )
    assert (
        revised.revision.revision_cause
        == NightRevisionCause.CORRECTED_VENDOR_REPORT
    )
    assert "vendor_report_pending" not in revised.revision.quality_flags
    assert revised.episode.state == NightEpisodeState.REVISED
    assert repository.get_night_episode_revision(
        NAMESPACE,
        night_episode_revision_id=first.revision.night_episode_revision_id,
    ) == first.revision

    late_observation_id = _observation(
        repository,
        suffix="late-after-watermark",
        event_at=end - timedelta(minutes=30),
        received_at=awaiting.episode.allowed_lateness_watermark_at
        + timedelta(minutes=1),
    )
    late = service.associate_observation(
        NAMESPACE,
        observation_id=late_observation_id,
        associated_at=awaiting.episode.allowed_lateness_watermark_at
        + timedelta(minutes=2),
    )
    assert late.revision is not None
    assert late.revision.revision_number == 3
    assert late.revision.revision_cause == NightRevisionCause.LATE_OBSERVATION
    assert "late_after_watermark" in late.revision.quality_flags

    changed_report_id = _register_report(
        repository,
        suffix="changed-vendor-report",
        local_report_date=date(2026, 7, 30),
        fetched_at=deadline + timedelta(hours=4),
        content=b'{"sleep":"corrected"}',
    )
    changed = service.associate_source_report(
        NAMESPACE,
        source_report_version_id=changed_report_id,
        associated_at=deadline + timedelta(hours=4, minutes=1),
    )
    assert changed.revision is not None
    assert changed.revision.revision_number == 4
    assert (
        changed.revision.revision_cause
        == NightRevisionCause.CORRECTED_VENDOR_REPORT
    )


def test_time_fallback_without_observations_publishes_data_insufficient() -> None:
    repository, _, _ = _repository()
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 8, 1, 22, 0, tzinfo=zone)
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    service = _service(repository, minimum_dwell=0)
    fallback_start = LifecycleTrigger(
        trigger_id="fallback-start",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.TIME_FALLBACK,
        kind=LifecycleTriggerKind.FALLBACK_START,
        occurred_at=start,
        received_at=start,
        correlation_id="correlation:fallback-start",
    )
    opened = service.process_trigger(
        NAMESPACE,
        trigger=fallback_start,
        device_binding_id="binding-v1",
    )
    assert (
        opened.episode.collection_window_derivation.value
        == "time_fallback"
    )
    fallback_end = LifecycleTrigger(
        trigger_id="fallback-end",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.TIME_FALLBACK,
        kind=LifecycleTriggerKind.FALLBACK_END,
        occurred_at=start + timedelta(hours=9),
        received_at=start + timedelta(hours=9),
        correlation_id="correlation:fallback-end",
    )
    awaiting = service.process_trigger(NAMESPACE, trigger=fallback_end)
    published = service.publish_report_deadline(
        NAMESPACE,
        night_episode_id=opened.episode.night_episode_id,
        trigger_id="fallback-deadline",
        published_at=awaiting.episode.report_deadline_at,
    )
    assert published.revision is not None
    assert (
        published.revision.data_sufficiency
        == DataSufficiency.DATA_INSUFFICIENT
    )
    assert "data_insufficient" in published.revision.quality_flags

    unauthorized = LifecycleTrigger(
        trigger_id="unauthorized-command",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
        kind=LifecycleTriggerKind.ACTIVATE,
        occurred_at=start + timedelta(days=1),
        received_at=start + timedelta(days=1),
        actor_id="operator",
        authorization_id="not-granted",
        correlation_id="correlation:unauthorized",
    )
    with pytest.raises(LifecycleAuthorizationError):
        service.process_trigger(
            NAMESPACE,
            trigger=unauthorized,
            device_binding_id="binding-v1",
        )

    off_schedule = LifecycleTrigger(
        trigger_id="off-schedule-fallback",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.TIME_FALLBACK,
        kind=LifecycleTriggerKind.FALLBACK_START,
        occurred_at=start + timedelta(days=1, hours=8),
        received_at=start + timedelta(days=1, hours=8),
        correlation_id="correlation:off-schedule",
    )
    with pytest.raises(
        InvalidLifecycleTransitionError,
        match="outside the pinned schedule window",
    ):
        service.process_trigger(
            NAMESPACE,
            trigger=off_schedule,
            device_binding_id="binding-v1",
        )


def test_concurrent_reanalysis_has_monotonic_parent_chain_and_cas_pointer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "night-revision.sqlite3"
    policy = _raw_policy()
    first_repo, first_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 7, 30, 22, 0, tzinfo=zone)
    _bind(
        first_repo,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    first_service = _service(first_repo, minimum_dwell=0)
    start_id = _observation(
        first_repo,
        suffix="concurrent-start",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
    )
    opened = first_service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=start + timedelta(seconds=1),
    )
    end_id = _observation(
        first_repo,
        suffix="concurrent-end",
        event_at=start + timedelta(hours=8),
        bed_state=BedPresenceState.OUT_OF_BED,
    )
    awaiting = first_service.process_observation(
        NAMESPACE,
        observation_id=end_id,
        processed_at=start + timedelta(hours=8, seconds=1),
    )
    first_service.publish_report_deadline(
        NAMESPACE,
        night_episode_id=opened.episode.night_episode_id,
        trigger_id="concurrent-deadline",
        published_at=awaiting.episode.report_deadline_at,
    )
    second_repo, second_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    second_service = _service(second_repo, minimum_dwell=0)
    triggers = (
        _command(
            trigger_id="reanalysis-a",
            kind=LifecycleTriggerKind.REANALYZE,
            occurred_at=awaiting.episode.report_deadline_at
            + timedelta(minutes=1),
        ),
        _command(
            trigger_id="reanalysis-b",
            kind=LifecycleTriggerKind.REANALYZE,
            occurred_at=awaiting.episode.report_deadline_at
            + timedelta(minutes=2),
        ),
    )

    def publish(args):
        service, trigger = args
        return service.request_reanalysis(
            NAMESPACE,
            night_episode_id=opened.episode.night_episode_id,
            trigger=trigger,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                publish,
                ((first_service, triggers[0]), (second_service, triggers[1])),
            )
        )
    assert sorted(result.revision.revision_number for result in results) == [2, 3]
    pointer = first_repo.get_current_night_revision(
        NAMESPACE,
        night_episode_id=opened.episode.night_episode_id,
    )
    assert pointer.current_revision_number == 3
    revision_3 = first_repo.get_night_episode_revision(
        NAMESPACE,
        night_episode_revision_id=pointer.current_revision_id,
    )
    revision_2 = first_repo.get_night_episode_revision(
        NAMESPACE,
        night_episode_revision_id=revision_3.parent_revision_id,
    )
    assert revision_2.revision_number == 2
    assert revision_3.parent_revision_id == revision_2.night_episode_revision_id
    first_connection.close()
    second_connection.close()


def test_restart_recovery_version_pins_and_expired_lease_reclaim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.sqlite3"
    policy = _raw_policy()
    repository, connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 7, 30, 22, 0, tzinfo=zone)
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    service_v1 = _service(repository, adapter_version="1.0.0")
    start_id = _observation(
        repository,
        suffix="restart-start",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
        adapter_version="1.0.0",
    )
    opened = service_v1.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=start + timedelta(seconds=1),
    )
    stale_lease = repository.acquire_subject_lifecycle_lease(
        NAMESPACE,
        subject_id="elder-1",
        lease_owner="crashed-worker",
        lease_token="crashed-token",
        now=start + timedelta(minutes=1),
        lease_duration=timedelta(seconds=1),
    )
    assert stale_lease is not None
    connection.close()

    restarted_repo, restarted_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=policy,
    )
    service_v2 = _service(
        restarted_repo,
        active_boundary_version="boundary-v2",
        adapter_version="2.0.0",
        transition_version="transition-v2",
    )
    recovered = service_v2.recover_subject(
        NAMESPACE,
        subject_id="elder-1",
    )
    assert recovered.episode is not None
    assert (
        recovered.episode.pinned_adapter_versions["perceptor-adapter"]
        == "1.0.0"
    )
    assert (
        recovered.episode.pinned_policy_versions["night_boundary"]
        == "boundary-v1"
    )
    assert (
        recovered.episode.pinned_policy_versions["lifecycle_transition"]
        == "transition-v1"
    )
    reclaimed = restarted_repo.acquire_subject_lifecycle_lease(
        NAMESPACE,
        subject_id="elder-1",
        lease_owner="restart-worker",
        lease_token="restart-token",
        now=start + timedelta(minutes=2),
        lease_duration=timedelta(seconds=5),
    )
    assert reclaimed is not None
    restarted_repo.release_subject_lifecycle_lease(NAMESPACE, reclaimed)

    v2_observation = _observation(
        restarted_repo,
        suffix="restart-v2-same-night",
        event_at=start + timedelta(hours=1),
        adapter_version="2.0.0",
    )
    pending = service_v2.associate_observation(
        NAMESPACE,
        observation_id=v2_observation,
        associated_at=start + timedelta(hours=1, seconds=2),
    )
    assert pending.status == EpisodeAggregationStatus.PENDING_ASSOCIATION
    assert pending.reason_code == "pinned_adapter_version_mismatch"
    fallback_end = LifecycleTrigger(
        trigger_id="restart-fallback-end",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.TIME_FALLBACK,
        kind=LifecycleTriggerKind.FALLBACK_END,
        occurred_at=start + timedelta(hours=10),
        received_at=start + timedelta(hours=10),
        correlation_id="correlation:restart-fallback-end",
    )
    ended_with_pinned_v1 = service_v2.process_trigger(
        NAMESPACE,
        trigger=fallback_end,
    )
    assert (
        ended_with_pinned_v1.episode.state
        == NightEpisodeState.AWAITING_REPORT
    )
    restarted_connection.close()


def test_state_and_outbox_roll_back_together() -> None:
    repository, connection, _ = _repository()
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 7, 30, 22, 0, tzinfo=zone)
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=start - timedelta(days=1),
    )
    observation_id = _observation(
        repository,
        suffix="outbox-rollback",
        event_at=start,
        bed_state=BedPresenceState.IN_BED,
    )
    connection.execute(
        """
        CREATE TRIGGER fail_night_episode_outbox
        BEFORE INSERT ON sleep_domain_domain_outbox
        BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END
        """
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        _service(repository).process_observation(
            NAMESPACE,
            observation_id=observation_id,
            processed_at=start + timedelta(seconds=1),
        )
    assert repository.count_rows("sleep_domain_night_episodes") == 0
    assert repository.count_rows("sleep_domain_monitoring_snapshots") == 0
    assert repository.count_rows(
        "sleep_domain_lifecycle_transition_receipts"
    ) == 0
    assert repository.count_rows("sleep_domain_domain_outbox") == 0
