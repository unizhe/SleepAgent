from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from sleepagent.integrations.perceptor import (
    AuthorizedHumanAcceptanceApproval,
    AutomatedFindingStatus,
    IdempotencyProbeKind,
    RealAcceptanceEvidenceError,
    RealAcceptanceStage,
    RealAcceptanceStageEvidence,
    RealAcceptanceStatus,
    RealPerceptorAcceptanceAuditor,
    RealPerceptorAcceptanceManifest,
    RedactedAcceptanceArtifact,
    ScopedIdempotencyProbe,
    device_scope_sha256_for_bindings,
)
from sleepagent.sleep_domain import (
    AdapterCapability,
    AdapterDeploymentStatus,
    AdapterDescriptor,
    AlgorithmVersionValue,
    AnalysisRevision,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisStatus,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    CollectionWindowDerivation,
    ConfidenceValue,
    CurrentRisk,
    DataMode,
    DataSufficiency,
    DeterministicQualityAssessment,
    DeterministicSourceScope,
    DeviceBinding,
    DeviceBindingReference,
    DeviceBindingStatus,
    DomainEvent,
    DomainEventType,
    HeartRatePayload,
    MissingState,
    MissingnessState,
    MovementPayload,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeState,
    NightRevisionCause,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    QualityState,
    RawIngressRecord,
    RespiratoryRatePayload,
    RiskState,
    RoleViewStatus,
    SignatureVerificationState,
    SleepObservation,
    SleepDomainRepository,
    SleepStageIntervalPayload,
    SleepStageState,
    SourceKind,
    TimezoneStatus,
    VendorSleepProfileMetricPayload,
)
from sleepagent.sleep_domain.repository import SourceReportVersion


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
ARTIFACT_SHA = "a" * 64
CONFIG_SHA = "b" * 64
PROFILE_SHA = "c" * 64
DEVICE_SHA = "d" * 64


def _artifact(scheme: str, name: str, marker: str) -> RedactedAcceptanceArtifact:
    return RedactedAcceptanceArtifact(
        reference=f"{scheme}://real-night/{name}",
        sha256=marker * 64,
        captured_at=NOW,
    )


def _stage_evidence(*, populated: bool) -> tuple[RealAcceptanceStageEvidence, ...]:
    return tuple(
        RealAcceptanceStageEvidence(
            stage=stage,
            evidence=(
                (
                    RedactedAcceptanceArtifact(
                        reference=f"evidence://real-night/{stage.value}",
                        sha256=(
                            PROFILE_SHA
                            if stage
                            == RealAcceptanceStage.PUSH_AUTHENTICATION
                            else "e" * 64
                        ),
                        captured_at=NOW,
                    ),
                )
                if populated
                else ()
            ),
            test_results=(
                (_artifact("test-result", stage.value, "f"),)
                if populated
                else ()
            ),
        )
        for stage in RealAcceptanceStage
    )


def _probe(
    kind: IdempotencyProbeKind,
    resource_id: str,
) -> ScopedIdempotencyProbe:
    keys = (
        {
            "raw_ingress_records",
            "canonical_observations",
            "night_episodes",
            "night_episode_revisions",
            "domain_events",
        }
        if kind == IdempotencyProbeKind.DUPLICATE_PUSH
        else {
            "raw_ingress_records",
            "source_reports",
            "canonical_observations",
            "night_episode_revisions",
            "domain_events",
        }
    )
    counts = {key: 1 for key in keys}
    return ScopedIdempotencyProbe(
        probe_kind=kind,
        first_resource_id=resource_id,
        repeated_resource_id=resource_id,
        counts_before=counts,
        counts_after=counts,
        test_result=_artifact(
            "test-result",
            kind.value,
            "1" if kind == IdempotencyProbeKind.DUPLICATE_PUSH else "2",
        ),
        performed_at=NOW,
    )


def _manifest(
    *,
    populated_evidence: bool,
    push_ids: tuple[str, ...] = (),
    duplicate_probe: ScopedIdempotencyProbe | None = None,
) -> RealPerceptorAcceptanceManifest:
    return RealPerceptorAcceptanceManifest(
        manifest_id="real-night-manifest-1",
        namespace_id="live:perceptor-real-gate",
        adapter_version="1.0.0",
        adapter_artifact_sha256=ARTIFACT_SHA,
        configuration_fingerprint=CONFIG_SHA,
        compatibility_profile_id="perceptor-live-profile-v1",
        compatibility_profile_sha256=PROFILE_SHA,
        device_scope_sha256=DEVICE_SHA,
        local_sleep_date=date(2026, 7, 29),
        push_raw_ingress_record_ids=push_ids,
        stage_evidence=_stage_evidence(populated=populated_evidence),
        duplicate_push_probe=duplicate_probe,
        created_at=NOW,
    )


def _repository() -> MagicMock:
    repository = MagicMock(spec=SleepDomainRepository)
    repository.get_adapter_descriptor.return_value = None
    repository.get_raw_record.return_value = None
    repository.get_night_episode.return_value = None
    repository.get_night_episode_revision.return_value = None
    return repository


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id="perceptor-v1",
        provider_id="perceptor",
        adapter_version="1.0.0",
        contract_version="sleep-domain.v1",
        supported_data_modes={DataMode.LIVE},
        supported_device_types=("millimeter_wave_radar",),
        supported_provider_account_ids=("opaque-account",),
        capabilities=(),
        adapter_artifact_sha256=ARTIFACT_SHA,
        configuration_fingerprint=CONFIG_SHA,
        output_observation_schema_versions=("sleep_observation.v1",),
        deployment_status=AdapterDeploymentStatus.ENABLED,
        activated_at=NOW,
    )


def _push_raw(raw_id: str) -> RawIngressRecord:
    return RawIngressRecord(
        raw_ingress_record_id=raw_id,
        data_mode=DataMode.LIVE,
        provider_id="perceptor",
        provider_account_id="opaque-account",
        event_type="VitalSignsDataEvent",
        message_id="opaque-message",
        request_signed_at=NOW - timedelta(seconds=1),
        received_at=NOW,
        signature_profile="perceptor-live-profile-v1",
        signature_verification=SignatureVerificationState.VERIFIED,
        idempotency_identity="message-id.v1:opaque-message",
        idempotency_version="provider-message-id.v1",
        pre_normalization_payload_sha256="9" * 64,
        encrypted_payload_reference="db:sleep_domain_raw_inbox:opaque",
        content_type="application/json",
        payload_size_bytes=128,
        retention_deadline=NOW + timedelta(days=1),
    )


def _report_raw(raw_id: str) -> RawIngressRecord:
    return RawIngressRecord(
        raw_ingress_record_id=raw_id,
        data_mode=DataMode.LIVE,
        provider_id="perceptor",
        provider_account_id="opaque-account",
        event_type="PerceptorSleepReportPull",
        message_id="pull:opaque",
        received_at=NOW + timedelta(hours=8),
        signature_verification=SignatureVerificationState.NOT_PROVIDED,
        idempotency_identity="perceptor-sleep-report.v1:opaque",
        idempotency_version="perceptor_pull.v1",
        pre_normalization_payload_sha256="8" * 64,
        encrypted_payload_reference="db:sleep_domain_raw_inbox:report",
        content_type="application/json",
        payload_size_bytes=512,
        retention_deadline=NOW + timedelta(days=1),
    )


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(
            state=AvailabilityState.NOT_PROVIDED,
            reason="not_provided",
        ),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED,
            reason="not_provided",
        ),
        calibration=CalibrationValue(
            state=AvailabilityState.NOT_PROVIDED,
            description="not_provided",
        ),
        completeness=1.0,
    )


def _observation(
    *,
    observation_id: str,
    observation_type: ObservationType,
    payload: object,
    raw: RawIngressRecord,
    source_kind: SourceKind,
) -> SleepObservation:
    return SleepObservation(
        observation_id=observation_id,
        data_mode=DataMode.LIVE,
        observation_type=observation_type,
        payload=payload,
        subject_id="subject-real",
        device_id="device-real",
        device_binding_id="binding-real",
        binding_version=1,
        measurement_at=NOW,
        received_at=NOW + timedelta(seconds=1),
        timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
        source_kind=source_kind,
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="opaque-account",
            adapter_id="perceptor-v1",
            adapter_version="1.0.0",
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
        ),
        source_key=f"source:{observation_id}",
        idempotency_key=f"idempotency:{observation_id}",
    )


def test_missing_real_scope_stays_pending_without_reading_raw_payload() -> None:
    repository = _repository()
    manifest = _manifest(populated_evidence=False)

    bundle = RealPerceptorAcceptanceAuditor(repository).build_bundle(
        manifest,
        audited_at=NOW,
    )

    assert {item.status for item in bundle.automated_audit.findings} == {
        AutomatedFindingStatus.PENDING
    }
    assert {item.status for item in bundle.report.verdicts} == {
        RealAcceptanceStatus.PENDING
    }
    assert bundle.report.duplicate_push_checked is False
    repository.load_raw_payload.assert_not_called()


def test_passed_transport_and_push_auth_still_require_human_approval() -> None:
    raw_id = "raw:real-push"
    repository = _repository()
    repository.get_adapter_descriptor.return_value = _descriptor()
    repository.get_raw_record.return_value = _push_raw(raw_id)
    manifest = _manifest(
        populated_evidence=True,
        push_ids=(raw_id,),
        duplicate_probe=_probe(IdempotencyProbeKind.DUPLICATE_PUSH, raw_id),
    )

    bundle = RealPerceptorAcceptanceAuditor(repository).build_bundle(
        manifest,
        audited_at=NOW,
    )

    assert (
        bundle.automated_audit.finding_for(
            RealAcceptanceStage.TRANSPORT
        ).status
        == AutomatedFindingStatus.PASSED
    )
    assert (
        bundle.automated_audit.finding_for(
            RealAcceptanceStage.PUSH_AUTHENTICATION
        ).status
        == AutomatedFindingStatus.PASSED
    )
    verdict = next(
        item
        for item in bundle.report.verdicts
        if item.stage == RealAcceptanceStage.TRANSPORT
    )
    assert verdict.status == RealAcceptanceStatus.PENDING
    assert "authorized_human_stage_approval_required" in verdict.blocking_reasons
    assert bundle.report.duplicate_push_checked is True
    repository.load_raw_payload.assert_not_called()


def test_human_approval_cannot_override_pending_or_change_audit_scope() -> None:
    repository = _repository()
    manifest = _manifest(populated_evidence=False)
    auditor = RealPerceptorAcceptanceAuditor(repository)
    audit = auditor.audit(manifest, audited_at=NOW)
    approval = AuthorizedHumanAcceptanceApproval(
        approval_id="approval-1",
        manifest_sha256=audit.manifest_sha256,
        automated_audit_sha256=audit.audit_sha256,
        automated_audit_created_at=audit.created_at,
        approved_stages=(RealAcceptanceStage.NIGHT_EPISODE,),
        reviewed_by_actor_id="authorized-reviewer",
        authorization_reference=_artifact(
            "approval", "reviewer-authorization", "3"
        ),
        approval_reference=_artifact("approval", "decision", "4"),
        reviewed_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(
        RealAcceptanceEvidenceError,
        match="cannot override",
    ):
        auditor.build_bundle(manifest, approval=approval)

    changed_digest = approval.model_copy(
        update={"automated_audit_sha256": "5" * 64}
    )
    with pytest.raises(
        RealAcceptanceEvidenceError,
        match="not bound",
    ):
        auditor.build_bundle(manifest, approval=changed_digest)


def test_digest_bound_human_can_approve_only_passing_adapter_stages() -> None:
    raw_id = "raw:real-push"
    repository = _repository()
    repository.get_adapter_descriptor.return_value = _descriptor()
    repository.get_raw_record.return_value = _push_raw(raw_id)
    manifest = _manifest(
        populated_evidence=True,
        push_ids=(raw_id,),
        duplicate_probe=_probe(IdempotencyProbeKind.DUPLICATE_PUSH, raw_id),
    )
    auditor = RealPerceptorAcceptanceAuditor(repository)
    audit = auditor.audit(manifest, audited_at=NOW)
    approval = AuthorizedHumanAcceptanceApproval(
        approval_id="approval-push-1",
        manifest_sha256=audit.manifest_sha256,
        automated_audit_sha256=audit.audit_sha256,
        automated_audit_created_at=audit.created_at,
        approved_stages=(
            RealAcceptanceStage.TRANSPORT,
            RealAcceptanceStage.PUSH_AUTHENTICATION,
        ),
        approved_capabilities=(AdapterCapability.PUSH,),
        reviewed_by_actor_id="authorized-reviewer",
        authorization_reference=_artifact(
            "approval", "reviewer-authorization", "3"
        ),
        approval_reference=_artifact("approval", "push-decision", "4"),
        reviewed_at=NOW + timedelta(minutes=1),
    )

    bundle = auditor.build_bundle(manifest, approval=approval)

    statuses = {
        item.stage: item.status for item in bundle.report.verdicts
    }
    assert statuses[RealAcceptanceStage.TRANSPORT] == RealAcceptanceStatus.VERIFIED
    assert (
        statuses[RealAcceptanceStage.PUSH_AUTHENTICATION]
        == RealAcceptanceStatus.VERIFIED
    )
    assert statuses[RealAcceptanceStage.NIGHT_EPISODE] == RealAcceptanceStatus.PENDING
    push_receipt = next(
        item
        for item in bundle.report.capability_receipts
        if item.capability == AdapterCapability.PUSH
    )
    assert push_receipt.status.value == "verified"
    assert push_receipt.reviewed_by_actor_id == "authorized-reviewer"


def test_complete_committed_chain_needs_and_accepts_exact_human_approval() -> None:
    push_raw = _push_raw("raw:complete-push")
    report_raw = _report_raw("raw:complete-report")
    binding = DeviceBinding(
        data_mode=DataMode.LIVE,
        device_binding_id="binding-real",
        binding_version=1,
        device_id="device-real",
        provider_id="perceptor",
        provider_account_id="opaque-account",
        provider_device=ProviderDeviceIdentity(
            provider_device_name="opaque-device"
        ),
        subject_id="subject-real",
        timezone_name="Asia/Shanghai",
        effective_from=NOW - timedelta(hours=1),
        effective_until=NOW + timedelta(days=1),
        status=DeviceBindingStatus.ACTIVE,
        changed_by_actor_id="admin",
        change_reason="real acceptance",
        recorded_at=NOW - timedelta(hours=1),
    )
    observations = (
        _observation(
            observation_id="obs-heart",
            observation_type=ObservationType.HEART_RATE,
            payload=HeartRatePayload(value=62),
            raw=push_raw,
            source_kind=SourceKind.DEVICE_MEASURED,
        ),
        _observation(
            observation_id="obs-resp",
            observation_type=ObservationType.RESPIRATORY_RATE,
            payload=RespiratoryRatePayload(value=15),
            raw=push_raw,
            source_kind=SourceKind.DEVICE_MEASURED,
        ),
        _observation(
            observation_id="obs-movement",
            observation_type=ObservationType.MOVEMENT,
            payload=MovementPayload(value=1, unit="index"),
            raw=push_raw,
            source_kind=SourceKind.DEVICE_MEASURED,
        ),
        _observation(
            observation_id="obs-bed",
            observation_type=ObservationType.BED_PRESENCE,
            payload=BedPresencePayload(state=BedPresenceState.IN_BED),
            raw=push_raw,
            source_kind=SourceKind.DEVICE_MEASURED,
        ),
        _observation(
            observation_id="obs-stage",
            observation_type=ObservationType.SLEEP_STAGE_INTERVAL,
            payload=SleepStageIntervalPayload(
                stage=SleepStageState.DEEP,
                start_at=NOW,
                end_at=NOW + timedelta(hours=1),
            ),
            raw=report_raw,
            source_kind=SourceKind.VENDOR_DERIVED,
        ),
        _observation(
            observation_id="obs-profile",
            observation_type=ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
            payload=VendorSleepProfileMetricPayload(
                metric_name="sleep_score",
                value_state=AvailabilityState.KNOWN,
                value=80,
                unit="score",
            ),
            raw=report_raw,
            source_kind=SourceKind.VENDOR_DERIVED,
        ),
    )
    observation_ids = tuple(item.observation_id for item in observations)
    report_id = "source-report-real"
    revision_id = "night-revision-real"
    episode_id = "night-episode-real"
    report_hash = "7" * 64
    episode = NightEpisode(
        night_episode_id=episode_id,
        data_mode=DataMode.LIVE,
        subject_id="subject-real",
        timezone_name="Asia/Shanghai",
        local_sleep_date=date(2026, 7, 29),
        night_key="subject-real:2026-07-29",
        collection_start_at=NOW,
        collection_end_at=NOW + timedelta(hours=8),
        collection_window_derivation=CollectionWindowDerivation.VERIFIED_IN_BED,
        binding_references=(
            DeviceBindingReference(
                device_binding_id=binding.device_binding_id,
                binding_version=binding.binding_version,
                device_id=binding.device_id,
            ),
        ),
        observation_ids=observation_ids,
        source_report_references=(report_id,),
        night_episode_revision_ids=(revision_id,),
        data_sufficiency=DataSufficiency.SUFFICIENT,
        pinned_adapter_versions={"perceptor-v1": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={"quality": "quality.v1"},
        state=NightEpisodeState.CLOSED,
        current_night_episode_revision_id=revision_id,
        created_at=NOW,
        updated_at=NOW + timedelta(hours=8),
    )
    revision = NightEpisodeRevision(
        night_episode_revision_id=revision_id,
        night_episode_id=episode_id,
        data_mode=DataMode.LIVE,
        subject_id="subject-real",
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=observation_ids,
        source_report_references=(report_id,),
        source_report_sha256=(report_hash,),
        observation_set_sha256="6" * 64,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        created_at=NOW + timedelta(hours=8),
    )
    source_scope = DeterministicSourceScope(
        night_episode_id=episode_id,
        night_episode_revision_id=revision_id,
        observation_ids=observation_ids,
        observation_types=tuple(item.observation_type for item in observations),
        device_binding_ids=(binding.device_binding_id,),
        window_start_at=NOW,
        window_end_at=NOW + timedelta(hours=8),
    )
    quality = DeterministicQualityAssessment(
        assessment_id="quality-real",
        data_mode=DataMode.LIVE,
        subject_id="subject-real",
        night_episode_id=episode_id,
        quality_state=QualityState.SUFFICIENT,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        missingness_state=MissingnessState.COMPLETE,
        coverage_ratio=1.0,
        expected_bin_count=1,
        covered_bin_count=1,
        explicit_missing_interval_count=0,
        invalid_observation_count=0,
        stale=False,
        offline=False,
        clock_invalid=False,
        latest_observed_at=NOW + timedelta(hours=8),
        source_scope=source_scope,
        policy_version="quality.v1",
        reason_codes=("coverage_sufficient",),
        assessed_at=NOW + timedelta(hours=8),
    )
    risk = CurrentRisk(
        current_risk_id="risk-real",
        data_mode=DataMode.LIVE,
        subject_id="subject-real",
        night_episode_id=episode_id,
        risk_state=RiskState.NO_REVIEWED_SIGNAL,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        source_scope=source_scope,
        policy_version="risk.v1",
        observed_at=NOW + timedelta(hours=8),
        reason_codes=("no_reviewed_signal",),
        updated_at=NOW + timedelta(hours=8),
    )
    analysis = AnalysisRevision(
        analysis_revision_id="analysis-real",
        night_episode_id=episode_id,
        night_episode_revision_id=revision_id,
        night_episode_revision_number=1,
        data_mode=DataMode.LIVE,
        subject_id="subject-real",
        revision_number=1,
        analysis_run_id="internal-run-real",
        observation_set_sha256=revision.observation_set_sha256,
        source_report_sha256=(report_hash,),
        adapter_versions={"perceptor-v1": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"quality": "quality.v1"},
        data_sufficiency=DataSufficiency.SUFFICIENT,
        status=AnalysisStatus.READY,
        execution_mode="intelligent",
        result_resource_id="result-real",
        created_at=NOW + timedelta(hours=8, minutes=1),
    )
    views = tuple(
        AnalysisRoleView(
            role_view_id=f"view:{role.value}",
            analysis_revision_id=analysis.analysis_revision_id,
            night_episode_id=episode_id,
            night_episode_revision_id=revision_id,
            data_mode=DataMode.LIVE,
            subject_id="subject-real",
            role=role,
            status=RoleViewStatus.READY,
            product_agent_episode_id="internal-run-real",
            execution_mode="intelligent",
            content=f"{role.value} morning view",
            generated_at=NOW + timedelta(hours=8, minutes=2),
        )
        for role in AnalysisRole
    )
    source_report = SourceReportVersion(
        source_report_version_id=report_id,
        provider_id="perceptor",
        provider_account_id="opaque-account",
        provider_device_key="opaque-device",
        local_report_date=date(2026, 7, 29),
        report_version=1,
        content_sha256=report_hash,
        raw_ingress_record_id=report_raw.raw_ingress_record_id,
        is_empty=False,
        fetched_at=NOW + timedelta(hours=8),
        created=False,
    )
    event = DomainEvent(
        event_id="event-real",
        event_type=DomainEventType.MORNING_REPORT_READY,
        event_version="1",
        data_mode=DataMode.LIVE,
        aggregate_type="night_episode",
        aggregate_id=episode_id,
        aggregate_version=1,
        per_aggregate_sequence=1,
        delivery_offset=1,
        subject_id="subject-real",
        night_episode_id=episode_id,
        night_episode_revision_id=revision_id,
        event_occurred_at=NOW + timedelta(hours=8, minutes=2),
        persisted_at=NOW + timedelta(hours=8, minutes=2),
        correlation_id="correlation-real",
    )
    repository = _repository()
    repository.get_adapter_descriptor.return_value = _descriptor()
    raw_by_id = {
        push_raw.raw_ingress_record_id: push_raw,
        report_raw.raw_ingress_record_id: report_raw,
    }
    repository.get_raw_record.side_effect = (
        lambda _namespace, *, raw_ingress_record_id: raw_by_id.get(
            raw_ingress_record_id
        )
    )
    repository.get_night_episode.return_value = episode
    repository.get_night_episode_revision.return_value = revision
    observation_by_id = {
        item.observation_id: item for item in observations
    }
    repository.get_observation.side_effect = (
        lambda _namespace, *, observation_id: observation_by_id.get(
            observation_id
        )
    )
    repository.get_device_binding.return_value = binding
    repository.get_source_report_version.return_value = source_report
    repository.get_current_quality.return_value = quality
    repository.get_current_risk.return_value = risk
    repository.list_analysis_revisions.return_value = (analysis,)
    repository.list_analysis_role_views.return_value = views
    event_reader = MagicMock()
    event_reader.list_domain_events_after.return_value = (event,)
    event_reader.has_domain_events_after.return_value = False
    manifest = _manifest(
        populated_evidence=True,
        push_ids=(push_raw.raw_ingress_record_id,),
        duplicate_probe=_probe(
            IdempotencyProbeKind.DUPLICATE_PUSH,
            push_raw.raw_ingress_record_id,
        ),
    ).model_copy(
        update={
            "device_scope_sha256": device_scope_sha256_for_bindings((binding,)),
            "source_report_version_ids": (report_id,),
            "night_episode_id": episode_id,
            "night_episode_revision_id": revision_id,
            "report_repull_probe": _probe(
                IdempotencyProbeKind.REPORT_REPULL,
                report_id,
            ),
        }
    )
    auditor = RealPerceptorAcceptanceAuditor(
        repository,
        event_reader=event_reader,
    )
    automated = auditor.build_bundle(manifest, audited_at=NOW)
    assert {item.status for item in automated.automated_audit.findings} == {
        AutomatedFindingStatus.PASSED
    }
    assert {item.status for item in automated.report.verdicts} == {
        RealAcceptanceStatus.PENDING
    }
    approval = AuthorizedHumanAcceptanceApproval(
        approval_id="approval-complete",
        manifest_sha256=automated.automated_audit.manifest_sha256,
        automated_audit_sha256=automated.automated_audit.audit_sha256,
        automated_audit_created_at=automated.automated_audit.created_at,
        approved_stages=tuple(RealAcceptanceStage),
        approved_capabilities=(
            AdapterCapability.PUSH,
            AdapterCapability.PULL,
            AdapterCapability.SLEEP_REPORT,
        ),
        reviewed_by_actor_id="authorized-reviewer",
        authorization_reference=_artifact(
            "approval", "reviewer-authorization", "3"
        ),
        approval_reference=_artifact("approval", "complete-decision", "4"),
        reviewed_at=NOW + timedelta(minutes=1),
    )

    approved = auditor.build_bundle(manifest, approval=approval)

    assert {item.status for item in approved.report.verdicts} == {
        RealAcceptanceStatus.VERIFIED
    }
    assert approved.report.duplicate_push_checked is True
    assert approved.report.report_repull_checked is True
    serialized = approved.model_dump_json()
    assert push_raw.raw_ingress_record_id not in serialized
    assert push_raw.message_id not in serialized
    assert binding.subject_id not in serialized
    assert binding.device_id not in serialized
    repository.load_raw_payload.assert_not_called()


@pytest.mark.parametrize(
    "reference",
    [
        "evidence://fixture/night",
        "evidence://real-night/raw-payload",
        "evidence://real-night/item?token=secret",
        "file:///protected/evidence.json",
    ],
)
def test_non_live_or_sensitive_artifact_references_fail_closed(
    reference: str,
) -> None:
    with pytest.raises((ValidationError, RealAcceptanceEvidenceError)):
        RedactedAcceptanceArtifact(
            reference=reference,
            sha256="6" * 64,
            captured_at=NOW,
        )


def test_probe_rejects_changed_resource_or_scoped_counts() -> None:
    with pytest.raises(ValidationError, match="original resource"):
        ScopedIdempotencyProbe(
            probe_kind=IdempotencyProbeKind.DUPLICATE_PUSH,
            first_resource_id="raw:one",
            repeated_resource_id="raw:two",
            counts_before={
                "raw_ingress_records": 1,
                "canonical_observations": 4,
                "night_episodes": 1,
                "night_episode_revisions": 1,
                "domain_events": 1,
            },
            counts_after={
                "raw_ingress_records": 1,
                "canonical_observations": 4,
                "night_episodes": 1,
                "night_episode_revisions": 1,
                "domain_events": 1,
            },
            test_result=_artifact("test-result", "duplicate", "7"),
            performed_at=NOW,
        )
