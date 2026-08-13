from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sleepagent.domain import (
    AdapterCapability,
    AdapterDeploymentStatus,
    AdapterDescriptor,
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AnalysisRevision,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    CapabilityDeclaration,
    CapabilitySupport,
    CapabilityVerificationReceipt,
    CapabilityVerificationStatus,
    CareFollowupState,
    ConfidenceValue,
    DataMode,
    DataSufficiency,
    DeviceBinding,
    DeviceBindingReference,
    DeviceBindingStatus,
    DomainEvent,
    DomainEventType,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MonitoringState,
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
    ProviderDeviceIdentity,
    RawIngressRecordMetadata,
    QuarantineReason,
    ServiceMode,
    SignatureVerificationState,
    SleepObservation,
    SourceKind,
    TimezoneStatus,
    VerificationReviewerKind,
    bind_adapter_candidate,
)


UTC = timezone.utc
MEASURED_AT = datetime(2026, 7, 29, 23, 15, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 7, 29, 23, 15, 2, tzinfo=UTC)
SHA = "a" * 64


def _quality(
    missing_state: MissingState = MissingState.PRESENT,
) -> ObservationQuality:
    return ObservationQuality(
        missing_state=missing_state,
        confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
    )


def _provenance() -> ObservationProvenance:
    return ObservationProvenance(
        provider_id="perceptor",
        provider_account_id="account-1",
        adapter_id="perceptor-adapter",
        adapter_version="1.0.0",
        raw_ingress_record_id="raw-1",
        raw_payload_sha256=SHA,
    )


def _provider_device() -> ProviderDeviceIdentity:
    return ProviderDeviceIdentity(provider_device_name="imei-001")


def _candidate(
    *,
    data_mode: DataMode = DataMode.LIVE,
) -> AdapterObservationCandidate:
    return AdapterObservationCandidate(
        candidate_id="candidate-1",
        data_mode=data_mode,
        observation_type=ObservationType.HEART_RATE,
        payload=HeartRatePayload(value=68),
        source_kind=SourceKind.DEVICE_MEASURED,
        provider_id="perceptor",
        provider_account_id="account-1",
        provider_device=_provider_device(),
        request_signed_at=datetime(2026, 7, 29, 23, 14, 59, tzinfo=UTC),
        measurement_at=MEASURED_AT,
        event_occurred_at=datetime(2026, 7, 29, 23, 15, 1, tzinfo=UTC),
        received_at=RECEIVED_AT,
        source_timestamp_text="2026-07-29T23:15:00+00:00",
        timezone_status=TimezoneStatus.KNOWN,
        quality=_quality(),
        provenance=_provenance(),
        source_key="perceptor:msg-1:heart-rate",
        idempotency_key="perceptor:msg-1:heart-rate:sha",
    )


def _binding(
    *,
    data_mode: DataMode = DataMode.LIVE,
) -> DeviceBinding:
    return DeviceBinding(
        data_mode=data_mode,
        device_binding_id="binding-1",
        binding_version=7,
        device_id="device-internal-1",
        provider_id="perceptor",
        provider_account_id="account-1",
        provider_device=_provider_device(),
        subject_id="subject-1",
        timezone_name="Asia/Shanghai",
        effective_from=MEASURED_AT - timedelta(days=1),
        effective_until=MEASURED_AT + timedelta(days=1),
        status=DeviceBindingStatus.ACTIVE,
        changed_by_actor_id="admin-1",
        change_reason="authorized installation",
        recorded_at=MEASURED_AT - timedelta(days=1),
    )


def test_candidate_forbids_subject_binding_and_night_identity() -> None:
    candidate = _candidate()
    data = candidate.model_dump()

    for forbidden_name in (
        "subject_id",
        "device_binding_id",
        "binding_version",
        "night_episode_id",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AdapterObservationCandidate.model_validate(
                {**data, forbidden_name: "forbidden"}
            )


def test_sleep_observation_requires_subject_and_exact_binding_version() -> None:
    observation = bind_adapter_candidate(
        _candidate(),
        _binding(),
        observation_id="observation-1",
    )

    assert observation.subject_id == "subject-1"
    assert observation.device_binding_id == "binding-1"
    assert observation.binding_version == 7

    data = observation.model_dump()
    for required_name in ("subject_id", "device_binding_id", "binding_version"):
        incomplete = dict(data)
        incomplete.pop(required_name)
        with pytest.raises(ValidationError, match="Field required"):
            SleepObservation.model_validate(incomplete)


def test_binding_is_explicit_and_never_uses_receipt_time_as_event_time() -> None:
    candidate = _candidate()
    observation = bind_adapter_candidate(candidate, _binding())

    assert observation.request_signed_at == candidate.request_signed_at
    assert observation.measurement_at == candidate.measurement_at
    assert observation.event_occurred_at == candidate.event_occurred_at
    assert observation.received_at == candidate.received_at
    assert len(
        {
            observation.request_signed_at,
            observation.measurement_at,
            observation.event_occurred_at,
            observation.received_at,
        }
    ) == 4

    no_event_time = candidate.model_copy(
        update={"measurement_at": None, "event_occurred_at": None}
    )
    with pytest.raises(ValueError, match="needs measurement_at or event_occurred_at"):
        bind_adapter_candidate(no_event_time, _binding())


def test_binding_rejects_mode_device_and_effective_time_mismatch() -> None:
    with pytest.raises(ValueError, match="data_mode"):
        bind_adapter_candidate(
            _candidate(data_mode=DataMode.REPLAY),
            _binding(data_mode=DataMode.LIVE),
        )

    wrong_device = _binding().model_copy(
        update={
            "provider_device": ProviderDeviceIdentity(
                provider_device_name="imei-other"
            )
        }
    )
    with pytest.raises(ValueError, match="does not match"):
        bind_adapter_candidate(_candidate(), wrong_device)

    future_binding = _binding().model_copy(
        update={"effective_from": MEASURED_AT + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="precedes"):
        bind_adapter_candidate(_candidate(), future_binding)


def test_source_kind_vocabulary_has_no_raw_payload_category() -> None:
    assert {item.value for item in SourceKind} == {
        "device_measured",
        "vendor_derived",
        "user_reported",
        "externally_reported",
        "future_model_derived",
    }
    with pytest.raises(ValidationError):
        AdapterObservationCandidate.model_validate(
            {**_candidate().model_dump(), "source_kind": "raw_payload"}
        )


def test_quality_requires_explicit_confidence_algorithm_and_calibration_state() -> None:
    base = {"missing_state": MissingState.PRESENT}
    for required_name in ("confidence", "algorithm_version", "calibration"):
        values = {
            **base,
            "confidence": ConfidenceValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            "algorithm_version": AlgorithmVersionValue(
                state=AvailabilityState.UNKNOWN
            ),
            "calibration": CalibrationValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
        }
        values.pop(required_name)
        with pytest.raises(ValidationError, match="Field required"):
            ObservationQuality.model_validate(values)

    with pytest.raises(ValidationError, match="known confidence requires value"):
        ConfidenceValue(state=AvailabilityState.KNOWN)


def test_canonical_provenance_forbids_legacy_payload_fields() -> None:
    values = _provenance().model_dump()
    for forbidden_name in ("raw_payload", "data_payload"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ObservationProvenance.model_validate(
                {**values, forbidden_name: {"secret": "must-not-cross"}}
            )


def test_contract_versions_and_extra_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        HeartRatePayload.model_validate(
            {
                "schema_version": "heart_rate_payload.v2",
                "observation_type": "heart_rate",
                "value": 60,
                "unit": "beats_per_minute",
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HeartRatePayload(value=60, vendor_extension=1)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _candidate().model_copy(
            update={"received_at": datetime(2026, 7, 29, 23, 15)}
        ).model_dump()
        AdapterObservationCandidate.model_validate(
            {
                **_candidate().model_dump(),
                "received_at": datetime(2026, 7, 29, 23, 15),
            }
        )


def test_payload_discriminator_must_match_envelope() -> None:
    with pytest.raises(ValidationError, match="must match envelope"):
        AdapterObservationCandidate.model_validate(
            {
                **_candidate().model_dump(),
                "observation_type": ObservationType.BED_PRESENCE,
            }
        )


def test_missing_payload_and_quality_state_cannot_disagree() -> None:
    candidate = _candidate()
    missing = MissingIntervalPayload(
        target_observation_type=ObservationType.HEART_RATE,
        missing_state=MissingState.INVALID,
        reason_code="invalid_vendor_value",
    )
    with pytest.raises(ValidationError, match="same missing_state"):
        AdapterObservationCandidate.model_validate(
            {
                **candidate.model_dump(),
                "observation_type": ObservationType.MISSING_INTERVAL,
                "payload": missing.model_dump(),
                "quality": _quality(MissingState.MISSING).model_dump(),
            }
        )


def test_adapter_descriptor_separates_capability_and_deployment_state() -> None:
    descriptor = AdapterDescriptor(
        adapter_id="perceptor-adapter",
        provider_id="perceptor",
        adapter_version="1.0.0",
        contract_version="sleep-domain.v1",
        supported_data_modes={DataMode.LIVE, DataMode.REPLAY},
        supported_device_types=("millimeter_wave_radar",),
        supported_provider_account_ids=("account-1",),
        capabilities=(
            CapabilityDeclaration(
                capability=AdapterCapability.SLEEP_REPORT,
                support=CapabilitySupport.YES,
                environment="production",
                verification_status=CapabilityVerificationStatus.PENDING,
            ),
        ),
        adapter_artifact_sha256=SHA,
        configuration_fingerprint=SHA,
        output_observation_schema_versions=("sleep_observation.v1",),
        deployment_status=AdapterDeploymentStatus.ENABLED,
    )

    assert descriptor.deployment_status == AdapterDeploymentStatus.ENABLED
    assert (
        descriptor.capabilities[0].verification_status
        == CapabilityVerificationStatus.PENDING
    )


def test_capability_cannot_self_promote_to_verified() -> None:
    values = dict(
        receipt_id="verification-1",
        data_mode=DataMode.LIVE,
        adapter_id="perceptor-adapter",
        adapter_version="1.0.0",
        adapter_artifact_sha256=SHA,
        configuration_fingerprint=SHA,
        capability=AdapterCapability.VITAL_PUSH,
        environment="production",
        status=CapabilityVerificationStatus.VERIFIED,
        evidence_references=("redacted-evidence:push-1",),
        evidence_sha256=(SHA,),
        test_result_references=("conformance:test-push-1",),
        test_result_sha256=(SHA,),
        conformance_result="passed",
        reviewed_at=RECEIVED_AT,
    )
    with pytest.raises(ValidationError, match="human reviewer"):
        CapabilityVerificationReceipt(
            **values,
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
        )

    receipt = CapabilityVerificationReceipt(
        **values,
        reviewer_kind=VerificationReviewerKind.HUMAN,
        reviewed_by_actor_id="reviewer-1",
    )
    assert receipt.status == CapabilityVerificationStatus.VERIFIED


def test_processing_receipt_requires_typed_quarantine_reason() -> None:
    values = dict(
        receipt_id="receipt-1",
        raw_ingress_record_id="raw-1",
        data_mode=DataMode.LIVE,
        stage=ProcessingStage.NORMALIZATION,
        outcome=ProcessingOutcome.QUARANTINED,
        occurred_at=RECEIVED_AT,
        actor_id="normalizer-worker",
        processor_id="perceptor-adapter",
        processor_version="1.0.0",
    )
    with pytest.raises(ValidationError, match="requires a reason"):
        ProcessingReceipt(**values)

    receipt = ProcessingReceipt(
        **values,
        quarantine_reason=QuarantineReason.UNKNOWN_FORMAT,
    )
    assert receipt.quarantine_reason == QuarantineReason.UNKNOWN_FORMAT


def test_night_episode_is_distinct_from_internal_analysis_episode() -> None:
    episode = NightEpisode(
        night_episode_id="night-1",
        data_mode=DataMode.LIVE,
        subject_id="subject-1",
        timezone_name="Asia/Shanghai",
        local_sleep_date=date(2026, 7, 29),
        night_key="subject-1:2026-07-29:boundary-policy-v1",
        collection_start_at=MEASURED_AT,
        collection_end_at=MEASURED_AT + timedelta(hours=8),
        collection_window_derivation="verified_in_bed",
        binding_references=(
            DeviceBindingReference(
                device_binding_id="binding-1",
                binding_version=7,
                device_id="device-1",
            ),
        ),
        observation_ids=("observation-1",),
        data_sufficiency=DataSufficiency.SUFFICIENT,
        pinned_adapter_versions={"perceptor-adapter": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={"night-boundary": "1.0.0"},
        state=NightEpisodeState.ANALYZED,
        created_at=RECEIVED_AT,
        updated_at=RECEIVED_AT,
    )
    night_revision = NightEpisodeRevision(
        night_episode_revision_id="night-revision-1",
        night_episode_id=episode.night_episode_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=("observation-1",),
        observation_set_sha256=SHA,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        created_at=RECEIVED_AT,
    )
    analysis = AnalysisRevision(
        analysis_revision_id="analysis-revision-1",
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=night_revision.night_episode_revision_id,
        night_episode_revision_number=1,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        analysis_run_id="internal-product-agent-episode-run-1",
        observation_set_sha256=SHA,
        adapter_versions={"perceptor-adapter": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"evidence": "1.0.0"},
        data_sufficiency=DataSufficiency.SUFFICIENT,
        created_at=RECEIVED_AT,
    )

    assert "analysis_run_id" not in NightEpisode.model_fields
    assert analysis.analysis_run_id == "internal-product-agent-episode-run-1"
    assert analysis.night_episode_id == episode.night_episode_id
    assert analysis.night_episode_revision_id == (
        night_revision.night_episode_revision_id
    )


def test_public_service_operation_and_event_contracts_are_strict() -> None:
    assert {state.value for state in MonitoringState} == {"dormant", "active"}
    assert {state.value for state in CareFollowupState} == {
        "none",
        "pending_feedback",
        "following_up",
        "completed",
        "ended",
    }
    assert {mode.value for mode in ServiceMode} == {
        "active",
        "follow_up",
        "dormant",
    }
    operation = Operation(
        operation_id="operation-1",
        data_mode=DataMode.REPLAY,
        operation_type="request_reanalysis",
        subject_id="replay-subject-1",
        service_principal_id="chatbot-1",
        actor_id="actor-1",
        target_resource_id="night-1",
        idempotency_key="idem-1",
        request_sha256=SHA,
        status=OperationStatus.SUCCEEDED,
        result_resource_id="analysis-revision-1",
        correlation_id="correlation-1",
        created_at=MEASURED_AT,
        updated_at=RECEIVED_AT,
    )
    event = DomainEvent(
        event_id="event-1",
        event_type=DomainEventType.NIGHT_EPISODE_REVISED,
        event_version="1",
        data_mode=operation.data_mode,
        aggregate_type="NightEpisode",
        aggregate_id="night-1",
        aggregate_version=2,
        per_aggregate_sequence=2,
        delivery_offset=91,
        subject_id=operation.subject_id,
        night_episode_id="night-1",
        night_episode_revision_id="night-revision-2",
        operation_id=operation.operation_id,
        event_occurred_at=MEASURED_AT,
        persisted_at=RECEIVED_AT,
        correlation_id=operation.correlation_id,
        attributes={"supersedes_revision": 1},
    )

    assert event.data_mode == DataMode.REPLAY
    assert event.attributes == {"supersedes_revision": 1}
    with pytest.raises(ValidationError):
        DomainEvent.model_validate(
            {
                **event.model_dump(),
                "attributes": {"raw_payload": {"must": "not nest"}},
            }
        )
