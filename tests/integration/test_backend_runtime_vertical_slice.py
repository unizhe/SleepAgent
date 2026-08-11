from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest

from sleepagent.persistence import RadarPersistenceStore
from sleepagent.simulation import (
    CanonicalReplayGenerator,
    canonical_replay_source_bytes,
    load_packaged_scenario,
)
from sleepagent.sleep_domain import (
    AdapterObservationCandidate,
    AdministrativeAccessPolicy,
    AdministrativeScope,
    DataMode,
    DeterministicFastPathService,
    DeterministicQualityPolicy,
    DeterministicRiskPolicy,
    DeviceBindingCommand,
    DeviceBindingService,
    DomainNamespace,
    EpisodeBoundaryPolicy,
    EpisodeVersionPins,
    FastPathEventPolicy,
    LifecycleAccessPolicy,
    LifecycleTransitionPolicy,
    NightEpisodeService,
    ObservationType,
    ProcessingOutcome,
    ProcessingReceipt,
    ProcessingStage,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    QualityState,
    RawIngressRecord,
    RawPayloadEncryptionPolicy,
    SignatureVerificationState,
    SleepDomainRepository,
    SleepObservation,
)
from sleepagent.sleep_domain.contracts import BedPresencePayload, BedPresenceState


UTC = timezone.utc


def _repository(
    namespace: DomainNamespace,
    *,
    provider_id: str,
    provider_account_id: str,
) -> tuple[SleepDomainRepository, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    repository = SleepDomainRepository(
        RadarPersistenceStore.connect_sqlite(connection),
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="simulation-integration-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=30),
            production=False,
        ),
    )
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=namespace,
            provider_account_id=provider_account_id,
            provider_id=provider_id,
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"fixture": "canonical-replay-integration"},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    return repository, connection


def _bind_synthetic_device(repository, namespace, scenario, first_event_at) -> None:
    identity = scenario.identity
    DeviceBindingService(
        repository,
        access_policy=AdministrativeAccessPolicy(
            {"simulation-admin": frozenset({AdministrativeScope.DEVICE_BINDING_WRITE})},
            authorization_ids={
                "simulation-admin": frozenset({"simulation-binding-auth"})
            },
        ),
    ).apply(
        namespace,
        DeviceBindingCommand(
            command_id=f"bind:{scenario.scenario_id}",
            data_mode=DataMode.REPLAY,
            device_binding_id=identity.device_binding_id,
            expected_binding_version=0,
            device_id=identity.device_id,
            provider_id=identity.provider_id,
            provider_account_id=identity.provider_account_id,
            provider_device=ProviderDeviceIdentity(
                provider_device_id=identity.provider_device_id
            ),
            subject_id=identity.subject_id,
            timezone_name=scenario.environment.timezone_name,
            effective_from=first_event_at - timedelta(days=1),
            actor_id="simulation-admin",
            authorization_id="simulation-binding-auth",
            change_reason="canonical replay integration fixture",
            requested_at=first_event_at - timedelta(days=1),
        ),
    )


def _persist_generated_observation(
    repository: SleepDomainRepository,
    namespace: DomainNamespace,
    scenario,
    observation: SleepObservation,
) -> None:
    raw_payload = canonical_replay_source_bytes(observation)
    raw = RawIngressRecord(
        raw_ingress_record_id=observation.provenance.raw_ingress_record_id,
        data_mode=DataMode.REPLAY,
        provider_id=observation.provenance.provider_id,
        provider_account_id=observation.provenance.provider_account_id,
        event_type=observation.observation_type.value,
        message_id=observation.source_key,
        measurement_at=observation.measurement_at,
        event_occurred_at=observation.event_occurred_at,
        received_at=observation.received_at,
        signature_profile="synthetic-nonrelease.v1",
        signature_verification=SignatureVerificationState.NOT_PROVIDED,
        idempotency_identity=observation.idempotency_key,
        idempotency_version="canonical-replay-generator.v1",
        pre_normalization_payload_sha256=(
            observation.provenance.raw_payload_sha256
        ),
        encrypted_payload_reference=(
            f"db:{observation.provenance.raw_ingress_record_id}"
        ),
        content_type="text/plain",
        payload_size_bytes=len(raw_payload),
        retention_deadline=observation.received_at + timedelta(days=30),
    )
    repository.intake_raw(
        namespace,
        raw,
        raw_payload=raw_payload,
        work_id=f"work:{observation.observation_id}",
        work_generation=1,
        work_json={"fixture": scenario.scenario_id},
        processing_intent_id=f"intent:{observation.observation_id}",
        processing_intent_json={"fixture": scenario.scenario_id},
        created_at=observation.received_at,
    )
    candidate = AdapterObservationCandidate(
        candidate_id=f"candidate:{observation.observation_id}",
        data_mode=observation.data_mode,
        observation_type=observation.observation_type,
        payload=observation.payload,
        source_kind=observation.source_kind,
        provider_id=scenario.identity.provider_id,
        provider_account_id=scenario.identity.provider_account_id,
        provider_device=ProviderDeviceIdentity(
            provider_device_id=scenario.identity.provider_device_id
        ),
        request_signed_at=observation.request_signed_at,
        measurement_at=observation.measurement_at,
        event_occurred_at=observation.event_occurred_at,
        received_at=observation.received_at,
        source_timestamp_text=observation.source_timestamp_text,
        timezone_status=observation.timezone_status,
        quality=observation.quality,
        provenance=observation.provenance,
        source_key=observation.source_key,
        idempotency_key=observation.idempotency_key,
    )
    repository.commit_candidate_observation(
        namespace,
        candidate=candidate,
        observation=observation,
        receipt=ProcessingReceipt(
            receipt_id=f"receipt:{observation.observation_id}",
            raw_ingress_record_id=raw.raw_ingress_record_id,
            data_mode=DataMode.REPLAY,
            stage=ProcessingStage.NORMALIZATION,
            outcome=ProcessingOutcome.SUCCEEDED,
            occurred_at=observation.received_at,
            actor_id="canonical-replay-generator",
            processor_id="canonical-replay-adapter",
            processor_version="1.0.0",
        ),
        committed_at=observation.received_at,
    )


def _episode_service(repository: SleepDomainRepository) -> NightEpisodeService:
    return NightEpisodeService(
        repository,
        boundary_policies={
            "boundary-v1": EpisodeBoundaryPolicy(
                policy_version="boundary-v1",
                rollover_local_minute=12 * 60,
                report_deadline_local_minute=10 * 60,
                maximum_episode_seconds=20 * 3600,
                allowed_lateness_seconds=2 * 3600,
            )
        },
        active_boundary_policy_version="boundary-v1",
        transition_policy=LifecycleTransitionPolicy(
            policy_version="transition-v1",
            debounce_seconds=0,
            minimum_active_dwell_seconds=0,
            minimum_dormant_dwell_seconds=0,
            minimum_collecting_dwell_seconds=0,
            manual_override_seconds=60,
            lease_seconds=5,
            fallback_start_local_minute=22 * 60,
            fallback_end_local_minute=8 * 60,
            fallback_tolerance_seconds=3600,
        ),
        access_policy=LifecycleAccessPolicy({}),
        default_version_pins=EpisodeVersionPins(
            adapter_versions={"canonical-replay-adapter": "1.0.0"},
            observation_schema_versions=("sleep_observation.v1",),
            policy_versions={
                "quality": "quality-v1",
                "risk": "risk-v1",
                "fast_path_event": "fast-path-event-v1",
            },
        ),
    )


def _fast_path(repository: SleepDomainRepository) -> DeterministicFastPathService:
    return DeterministicFastPathService(
        repository,
        quality_policies={
            "quality-v1": DeterministicQualityPolicy(policy_version="quality-v1")
        },
        risk_policies={
            "risk-v1": DeterministicRiskPolicy(policy_version="risk-v1")
        },
        event_policies={
            "fast-path-event-v1": FastPathEventPolicy(
                policy_version="fast-path-event-v1"
            )
        },
    )


@pytest.mark.parametrize(
    ("scenario_id", "expected_quality"),
    [
        ("golden-15-night", QualityState.SUFFICIENT),
        ("overlay-matrix", QualityState.DATA_INSUFFICIENT),
    ],
)
def test_generated_night_reaches_expected_real_fast_path_quality(
    scenario_id: str,
    expected_quality: QualityState,
) -> None:
    scenario = load_packaged_scenario(scenario_id)
    generated = CanonicalReplayGenerator().generate(scenario)
    first_night = tuple(
        item
        for item in generated.observations
        if ":night-1:" in (item.provenance.source_record_id or "")
    )
    namespace = DomainNamespace(
        scenario.environment.namespace_id,
        DataMode.REPLAY,
    )
    repository, connection = _repository(
        namespace,
        provider_id=scenario.identity.provider_id,
        provider_account_id=scenario.identity.provider_account_id,
    )
    try:
        first_event_at = min(
            item.measurement_at or item.event_occurred_at for item in first_night
        )
        _bind_synthetic_device(
            repository,
            namespace,
            scenario,
            first_event_at,
        )
        for observation in first_night:
            _persist_generated_observation(
                repository,
                namespace,
                scenario,
                observation,
            )

        bed_in = next(
            item
            for item in first_night
            if isinstance(item.payload, BedPresencePayload)
            and item.payload.state == BedPresenceState.IN_BED
        )
        bed_out = next(
            item
            for item in first_night
            if isinstance(item.payload, BedPresencePayload)
            and item.payload.state == BedPresenceState.OUT_OF_BED
        )
        bed_in_at = bed_in.event_occurred_at or bed_in.received_at
        before_bed = tuple(
            sorted(
                (
                    item
                    for item in first_night
                    if (item.measurement_at or item.event_occurred_at) < bed_in_at
                ),
                key=lambda item: (
                    item.measurement_at or item.event_occurred_at,
                    item.observation_id,
                ),
            )
        )
        middle = tuple(
            sorted(
                (
                    item
                    for item in first_night
                    if item.observation_id
                    not in {bed_in.observation_id, bed_out.observation_id}
                    and (item.measurement_at or item.event_occurred_at) >= bed_in_at
                    and (item.measurement_at or item.event_occurred_at)
                    < (bed_out.event_occurred_at or bed_out.received_at)
                ),
                key=lambda item: (
                    item.measurement_at or item.event_occurred_at,
                    item.observation_id,
                ),
            )
        )
        service = _episode_service(repository)
        for observation in before_bed:
            pending = service.process_observation(
                namespace,
                observation_id=observation.observation_id,
                processed_at=observation.received_at,
            )
            assert pending.pending_association is not None
        opened = service.process_observation(
            namespace,
            observation_id=bed_in.observation_id,
            processed_at=bed_in.received_at,
        )
        assert opened.episode is not None
        for observation in middle:
            service.process_observation(
                namespace,
                observation_id=observation.observation_id,
                processed_at=observation.received_at,
            )
        closed = service.process_observation(
            namespace,
            observation_id=bed_out.observation_id,
            processed_at=bed_out.received_at,
        )
        assert closed.episode is not None

        result = _fast_path(repository).evaluate(
            namespace,
            night_episode_id=closed.episode.night_episode_id,
            evaluation_id=f"{scenario_id}-night-1-default-policy",
            assessed_at=bed_out.received_at,
        )

        assert result.quality.quality_state == expected_quality
        if expected_quality == QualityState.SUFFICIENT:
            assert result.quality.coverage_ratio >= 0.99
            assert "coverage_below_minimum" not in result.quality.reason_codes
        else:
            assert result.quality.coverage_ratio < 0.75
            assert "coverage_below_minimum" in result.quality.reason_codes
            assert "explicit_missing_interval" in result.quality.reason_codes
        assert result.quality.source_scope.observation_types
        assert {
            ObservationType.HEART_RATE,
            ObservationType.RESPIRATORY_RATE,
            ObservationType.MOVEMENT,
        }.issubset(set(result.quality.source_scope.observation_types))
    finally:
        connection.close()
