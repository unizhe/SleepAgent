from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from sleepagent.radar_agent.persistence import (
    MIGRATION_VERSION,
    RADAR_AGENT_POSTGRES_MIGRATIONS,
    RadarPersistenceStore,
)
from sleepagent.sleep_domain import (
    AdapterCapability,
    AdapterConformanceSuite,
    AdapterDeniedError,
    AdapterDeploymentStatus,
    AdapterDescriptor,
    AdapterExecutionPolicy,
    AdapterExecutionStatus,
    AdapterInputEnvelope,
    AdapterLifecycleError,
    AdapterObservationCandidate,
    AdapterResolutionLock,
    AdapterVerificationError,
    CapabilitySupport,
    CapabilityVerificationReceipt,
    CapabilityVerificationStatus,
    CircuitState,
    ControlledAdapterRegistry,
    DataMode,
    DomainNamespace,
    ProviderAccountRecord,
    QuarantineReason,
    RawIngressRecord,
    RawPayloadEncryptionPolicy,
    SignatureVerificationState,
    SleepDomainRepository,
    StaticAdapterRegistration,
    VerificationReviewerKind,
    hardware_free_fixture_registration,
    perceptor_v1_registration,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
REPLAY = DomainNamespace("replay:adapter-conformance", DataMode.REPLAY)


def _repository() -> SleepDomainRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = RadarPersistenceStore.connect_sqlite(connection)
    return SleepDomainRepository(
        store,
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="test-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(hours=1),
            production=False,
        ),
    )


def _account(
    repository: SleepDomainRepository,
    registration: StaticAdapterRegistration,
    *,
    account_id: str = "fixture-account",
) -> None:
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=REPLAY,
            provider_account_id=account_id,
            provider_id=registration.descriptor.provider_id,
            configuration_fingerprint=(
                registration.descriptor.configuration_fingerprint
            ),
            status="enabled",
            metadata={"profile": "contract-test"},
            created_at=NOW,
        )
    )


def _raw(
    registration: StaticAdapterRegistration,
    *,
    account_id: str = "fixture-account",
    payload: bytes | None = None,
    raw_id: str = "raw-fixture-1",
) -> tuple[RawIngressRecord, bytes]:
    payload = payload or json.dumps(
        {"device_name": "fixture-device-1", "heart_rate": 63}
    ).encode("utf-8")
    record = RawIngressRecord(
        raw_ingress_record_id=raw_id,
        data_mode=DataMode.REPLAY,
        provider_id=registration.descriptor.provider_id,
        provider_account_id=account_id,
        event_type="hardware_free_fixture",
        message_id="fixture-message-1",
        request_signed_at=NOW - timedelta(seconds=2),
        measurement_at=NOW - timedelta(seconds=1),
        event_occurred_at=None,
        received_at=NOW,
        signature_profile="fixture-signature.v1",
        signature_verification=SignatureVerificationState.VERIFIED,
        idempotency_identity="fixture-message-1",
        idempotency_version="fixture-message.v1",
        pre_normalization_payload_sha256=hashlib.sha256(payload).hexdigest(),
        encrypted_payload_reference=f"db:raw:{raw_id}",
        content_type="application/json",
        payload_size_bytes=len(payload),
        retention_deadline=NOW + timedelta(hours=1),
    )
    return record, payload


def _registry(
    *registrations: StaticAdapterRegistration,
    policy: AdapterExecutionPolicy | None = None,
) -> tuple[ControlledAdapterRegistry, SleepDomainRepository]:
    repository = _repository()
    for index, registration in enumerate(registrations):
        account_id = (
            "fixture-account" if index == 0 else f"fixture-account-{index + 1}"
        )
        _account(repository, registration, account_id=account_id)
    registry = ControlledAdapterRegistry(
        namespace=REPLAY,
        repository=repository,
        allowlist=registrations,
        authorized_human_reviewers=frozenset({"authorized-reviewer"}),
        execution_policy=policy,
    )
    return registry, repository


def _enable(
    registry: ControlledAdapterRegistry,
    registration: StaticAdapterRegistration,
    *,
    suffix: str = "1",
) -> None:
    registry.enable(
        adapter_id=registration.descriptor.adapter_id,
        adapter_version=registration.descriptor.adapter_version,
        deployment_event_id=f"enable-{suffix}",
        actor_id="deployment-admin",
        reason="contract test enable",
        changed_at=NOW,
    )


def _resolve(
    registry: ControlledAdapterRegistry,
    registration: StaticAdapterRegistration,
    *,
    account_id: str = "fixture-account",
    lock_id: str = "lock-1",
    request_id: str = "resolution-request-1",
    version: str = "latest",
) -> AdapterResolutionLock:
    return registry.resolve(
        provider_id=registration.descriptor.provider_id,
        provider_account_id=account_id,
        environment="test",
        required_capabilities=(AdapterCapability.REALTIME_VITALS,),
        adapter_resolution_lock_id=lock_id,
        resolution_request_id=request_id,
        resolved_at=NOW + timedelta(seconds=1),
        adapter_id=registration.descriptor.adapter_id,
        version=version,
    )


def _input(
    registration: StaticAdapterRegistration,
    lock: AdapterResolutionLock,
    *,
    account_id: str = "fixture-account",
    payload: bytes | None = None,
    raw_id: str = "raw-fixture-1",
) -> AdapterInputEnvelope:
    raw, raw_payload = _raw(
        registration,
        account_id=account_id,
        payload=payload,
        raw_id=raw_id,
    )
    return AdapterInputEnvelope(
        data_mode=raw.data_mode,
        adapter_resolution_lock_id=lock.adapter_resolution_lock_id,
        environment=lock.environment,
        raw_record=raw,
        raw_payload=raw_payload,
    )


class _FunctionAdapter:
    def __init__(
        self,
        descriptor: AdapterDescriptor,
        function: Callable[
            [AdapterInputEnvelope],
            Sequence[AdapterObservationCandidate],
        ],
    ) -> None:
        self._descriptor = descriptor
        self._function = function

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def normalize(
        self,
        adapter_input: AdapterInputEnvelope,
    ) -> Sequence[AdapterObservationCandidate]:
        return self._function(adapter_input)


def _derived_registration(
    base: StaticAdapterRegistration,
    *,
    version: str,
    provider_id: str | None = None,
    adapter_id: str | None = None,
    account_id: str = "fixture-account",
    function: Callable[
        [AdapterInputEnvelope],
        Sequence[AdapterObservationCandidate],
    ],
) -> StaticAdapterRegistration:
    descriptor = base.descriptor.model_copy(
        update={
            "provider_id": provider_id or base.descriptor.provider_id,
            "adapter_id": adapter_id or base.descriptor.adapter_id,
            "adapter_version": version,
            "supported_provider_account_ids": (account_id,),
            "adapter_artifact_sha256": hashlib.sha256(
                f"fixture:{provider_id}:{version}".encode()
            ).hexdigest(),
        }
    )
    return StaticAdapterRegistration(
        descriptor=descriptor,
        implementation_factory=lambda: _FunctionAdapter(descriptor, function),
    )


def test_registry_control_migration_is_additive_and_reference_restricting() -> None:
    assert MIGRATION_VERSION == "020_legacy_authority_cutover"
    sql = RADAR_AGENT_POSTGRES_MIGRATIONS["011_adapter_registry_control"]
    for table in (
        "sleep_domain_adapter_deployment_events",
        "sleep_domain_capability_verification_receipts",
        "sleep_domain_adapter_resolution_locks",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_perceptor_v1_is_local_pending_and_has_no_waveform_claim() -> None:
    registration = perceptor_v1_registration(
        configuration_fingerprint="a" * 64,
        adapter_artifact_sha256="b" * 64,
        provider_account_ids=("perceptor-account",),
        environments=("test", "production"),
    )
    descriptor = registration.descriptor
    supported = {
        AdapterCapability.REALTIME_VITALS,
        AdapterCapability.VITAL_PUSH,
        AdapterCapability.BED_PRESENCE,
        AdapterCapability.MOVEMENT,
        AdapterCapability.ALERTS,
        AdapterCapability.SLEEP_REPORT,
        AdapterCapability.SLEEP_STAGES,
        AdapterCapability.HISTORICAL_RATE_SAMPLES,
    }
    for environment in ("test", "production"):
        declarations = {
            declaration.capability: declaration
            for declaration in descriptor.capabilities
            if declaration.environment == environment
        }
        assert set(declarations) == supported | {
            AdapterCapability.RAW_RESP_WAVEFORM
        }
        for capability in supported:
            assert declarations[capability].support == CapabilitySupport.YES
            assert (
                declarations[capability].verification_status
                == CapabilityVerificationStatus.PENDING
            )
        waveform = declarations[AdapterCapability.RAW_RESP_WAVEFORM]
        assert waveform.support == CapabilitySupport.NO
        assert (
            waveform.verification_status
            == CapabilityVerificationStatus.UNVERIFIED
        )
    assert registration.implementation_factory is not None
    assert registration.real_device_implementation is True
    assert descriptor.deployment_status == AdapterDeploymentStatus.REGISTERED
    assert (
        "local_push_normalization_only_no_network_calls"
        in descriptor.known_limitations
    )
    assert (
        "push_signing_profile_pending_real_confirmation"
        in descriptor.known_limitations
    )


def test_static_allowlist_is_deny_by_default_and_semver_is_strict() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, _ = _registry(fixture)
    _enable(registry, fixture)

    with pytest.raises(AdapterDeniedError, match="provider account"):
        registry.resolve(
            provider_id="unknown-provider",
            provider_account_id="fixture-account",
            environment="test",
            required_capabilities=(AdapterCapability.REALTIME_VITALS,),
            adapter_resolution_lock_id="unknown-lock",
            resolution_request_id="unknown-request",
            resolved_at=NOW,
        )
    with pytest.raises(AdapterDeniedError, match="strict MAJOR.MINOR.PATCH"):
        _resolve(registry, fixture, version="v1")
    assert not hasattr(registry, "register")
    assert not hasattr(registry, "upload_adapter")
    assert not hasattr(registry, "load_module")
    registry.close()


def test_adapter_effect_policy_cannot_grant_agents_writes_llm_or_assignment() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    policy = fixture.effect_policy
    assert policy.can_call_agents is False
    assert policy.can_write_external_state is False
    assert policy.can_send_raw_payload_to_llm is False
    assert policy.can_assign_subject_or_episode is False
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AdapterDescriptor.model_validate(
            {
                **fixture.descriptor.model_dump(),
                "agent_tool_access": True,
            }
        )


def test_perceptor_local_adapter_executes_without_network_path() -> None:
    registration = perceptor_v1_registration(
        configuration_fingerprint="a" * 64,
        adapter_artifact_sha256="b" * 64,
        provider_account_ids=("fixture-account",),
        environments=("test",),
    )
    registry, _ = _registry(registration)
    _enable(registry, registration)
    lock = _resolve(registry, registration)
    result = registry.execute(
        execution_receipt_id="perceptor-local-normalization",
        adapter_input=_input(
            registration,
            lock,
            payload=json.dumps(
                {
                    "message_id": "opaque-message",
                    "device_id": "opaque-device",
                    "device_name": "opaque-name",
                    "type": "VitalSignsDataEvent",
                    "data": {
                        "DateTime": int(NOW.timestamp() * 1000),
                        "HeartRate": 63,
                        "BreathRate": 15,
                        "BodyShake": 1,
                        "OnBed": 1,
                    },
                }
            ).encode(),
        ),
    )
    assert result.receipt.status == AdapterExecutionStatus.SUCCEEDED
    assert len(result.candidates) == 4
    assert all(
        "subject_id" not in candidate.model_dump()
        for candidate in result.candidates
    )
    registry.close()


def test_verification_binds_artifact_config_capability_environment_and_human() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, repository = _registry(fixture)
    descriptor = fixture.descriptor
    base = dict(
        receipt_id="verification-1",
        data_mode=DataMode.REPLAY,
        adapter_id=descriptor.adapter_id,
        adapter_version=descriptor.adapter_version,
        adapter_artifact_sha256=descriptor.adapter_artifact_sha256,
        configuration_fingerprint=descriptor.configuration_fingerprint,
        capability=AdapterCapability.REALTIME_VITALS,
        environment="test",
        status=CapabilityVerificationStatus.VERIFIED,
        evidence_references=("redacted-evidence:fixture",),
        evidence_sha256=("c" * 64,),
        test_result_references=("conformance:test-result",),
        test_result_sha256=("f" * 64,),
        conformance_result="passed",
        reviewer_kind=VerificationReviewerKind.HUMAN,
        reviewed_at=NOW,
    )
    with pytest.raises(AdapterVerificationError, match="authorized human"):
        registry.record_verification(
            CapabilityVerificationReceipt(
                **base,
                reviewed_by_actor_id="unauthorized-reviewer",
            )
        )
    with pytest.raises(AdapterVerificationError, match="artifact hash"):
        registry.record_verification(
            CapabilityVerificationReceipt(
                **{
                    **base,
                    "receipt_id": "verification-bad-hash",
                    "adapter_artifact_sha256": "d" * 64,
                },
                reviewed_by_actor_id="authorized-reviewer",
            )
        )
    with pytest.raises(AdapterVerificationError, match="cannot claim VERIFIED"):
        registry.record_verification(
            CapabilityVerificationReceipt(
                **base,
                reviewed_by_actor_id="authorized-reviewer",
            )
        )
    pending_values = {
        **base,
        "receipt_id": "verification-pending",
        "status": CapabilityVerificationStatus.PENDING,
        "reviewer_kind": VerificationReviewerKind.AUTOMATED,
    }
    receipt = registry.record_verification(
        CapabilityVerificationReceipt(
            **pending_values,
        )
    )
    assert receipt.status == CapabilityVerificationStatus.PENDING
    assert repository.count_rows(
        "sleep_domain_capability_verification_receipts"
    ) == 1
    registry.close()


def test_production_resolution_requires_exact_human_verified_capability() -> None:
    registration = perceptor_v1_registration(
        configuration_fingerprint="a" * 64,
        adapter_artifact_sha256="b" * 64,
        provider_account_ids=("fixture-account",),
        environments=("production",),
    )
    registry, _ = _registry(registration)
    _enable(registry, registration)
    descriptor = registration.descriptor

    with pytest.raises(AdapterDeniedError, match="no enabled allowlisted"):
        registry.resolve(
            provider_id=descriptor.provider_id,
            provider_account_id="fixture-account",
            environment="production",
            required_capabilities=(AdapterCapability.REALTIME_VITALS,),
            adapter_resolution_lock_id="production-lock-before-review",
            resolution_request_id="production-resolution-before-review",
            resolved_at=NOW,
            adapter_id=descriptor.adapter_id,
        )

    registry.record_verification(
        CapabilityVerificationReceipt(
            receipt_id="production-verification",
            data_mode=DataMode.REPLAY,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            adapter_artifact_sha256=descriptor.adapter_artifact_sha256,
            configuration_fingerprint=descriptor.configuration_fingerprint,
            capability=AdapterCapability.REALTIME_VITALS,
            environment="production",
            status=CapabilityVerificationStatus.VERIFIED,
            evidence_references=("device-evidence:production",),
            evidence_sha256=("c" * 64,),
            test_result_references=("conformance:production",),
            test_result_sha256=("d" * 64,),
            conformance_result="passed",
            reviewer_kind=VerificationReviewerKind.HUMAN,
            reviewed_by_actor_id="authorized-reviewer",
            reviewed_at=NOW,
        )
    )
    lock = registry.resolve(
        provider_id=descriptor.provider_id,
        provider_account_id="fixture-account",
        environment="production",
        required_capabilities=(AdapterCapability.REALTIME_VITALS,),
        adapter_resolution_lock_id="production-lock-after-review",
        resolution_request_id="production-resolution-after-review",
        resolved_at=NOW,
        adapter_id=descriptor.adapter_id,
    )
    assert (
        lock.capabilities[0].verification_status
        == CapabilityVerificationStatus.VERIFIED
    )
    registry.close()


def test_automated_conformance_evidence_cannot_self_promote_verified() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    with pytest.raises(ValidationError, match="human reviewer"):
        CapabilityVerificationReceipt(
            receipt_id="automated-promotion",
            data_mode=DataMode.REPLAY,
            adapter_id=fixture.descriptor.adapter_id,
            adapter_version=fixture.descriptor.adapter_version,
            adapter_artifact_sha256=fixture.descriptor.adapter_artifact_sha256,
            configuration_fingerprint=(
                fixture.descriptor.configuration_fingerprint
            ),
            capability=AdapterCapability.REALTIME_VITALS,
            environment="test",
            status=CapabilityVerificationStatus.VERIFIED,
            evidence_references=("conformance:report",),
            evidence_sha256=("e" * 64,),
            test_result_references=("conformance:test-result",),
            test_result_sha256=("f" * 64,),
            conformance_result="passed",
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
            reviewed_at=NOW,
        )


def test_fixture_conformance_outputs_candidates_only_and_stays_unverified() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, _ = _registry(fixture)
    _enable(registry, fixture)
    lock = _resolve(registry, fixture)
    adapter_input = _input(fixture, lock)

    report = AdapterConformanceSuite().run(fixture, adapter_input)
    result = registry.execute(
        execution_receipt_id="execution-1",
        adapter_input=adapter_input,
    )

    assert report.passed is True
    assert report.verification_status == CapabilityVerificationStatus.UNVERIFIED
    assert result.receipt.status == AdapterExecutionStatus.SUCCEEDED
    assert len(result.candidates) == 1
    serialized = result.candidates[0].model_dump_json()
    for forbidden in (
        '"subject_id"',
        '"device_binding_id"',
        '"binding_version"',
        '"night_episode_id"',
        '"raw_payload"',
        '"data_payload"',
    ):
        assert forbidden not in serialized
    assert result.candidates[0].data_mode == DataMode.REPLAY
    registry.close()


def test_resolution_lock_is_exact_persistent_and_has_no_episode_identity() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, repository = _registry(fixture)
    _enable(registry, fixture)
    lock = _resolve(registry, fixture)

    assert lock.adapter_version == "1.0.0"
    assert lock.configuration_fingerprint == (
        fixture.descriptor.configuration_fingerprint
    )
    assert repository.load_adapter_resolution_lock(
        REPLAY,
        adapter_resolution_lock_id=lock.adapter_resolution_lock_id,
    ) == lock
    assert registry.retained_reference_count(
        adapter_id=lock.adapter_id,
        adapter_version=lock.adapter_version,
    ) == 1
    serialized = lock.model_dump_json()
    assert "subject_id" not in serialized
    assert "night_episode" not in serialized
    registry.close()


def test_disabled_version_rejects_new_resolution_but_retained_lock_replays() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, _ = _registry(fixture)
    _enable(registry, fixture)
    lock = _resolve(registry, fixture)
    adapter_input = _input(fixture, lock)
    registry.disable(
        adapter_id=lock.adapter_id,
        adapter_version=lock.adapter_version,
        deployment_event_id="disable-1",
        actor_id="deployment-admin",
        reason="controlled disable",
        changed_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(AdapterDeniedError, match="no enabled allowlisted"):
        _resolve(
            registry,
            fixture,
            lock_id="lock-2",
            request_id="resolution-request-2",
        )
    replay = registry.execute(
        execution_receipt_id="retained-replay",
        adapter_input=adapter_input,
    )
    assert replay.receipt.status == AdapterExecutionStatus.SUCCEEDED
    assert registry.retained_reference_count(
        adapter_id=lock.adapter_id,
        adapter_version=lock.adapter_version,
    ) == 1
    registry.close()


def test_supersede_latest_resolution_and_explicit_rollback() -> None:
    old = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    old_adapter = old.implementation_factory()
    assert old_adapter is not None
    new = _derived_registration(
        old,
        version="1.1.0",
        function=old_adapter.normalize,
    )
    registry, _ = _registry(old, new)
    _enable(registry, old, suffix="old")
    _enable(registry, new, suffix="new")
    registry.supersede(
        adapter_id=old.descriptor.adapter_id,
        adapter_version=old.descriptor.adapter_version,
        superseded_by_version=new.descriptor.adapter_version,
        deployment_event_id="supersede-old",
        actor_id="deployment-admin",
        reason="new tested version",
        changed_at=NOW + timedelta(seconds=1),
    )
    latest = _resolve(registry, new)
    assert latest.adapter_version == "1.1.0"

    disabled, enabled = registry.rollback(
        adapter_id=old.descriptor.adapter_id,
        from_version="1.1.0",
        to_version="1.0.0",
        disable_event_id="rollback-disable-new",
        enable_event_id="rollback-enable-old",
        actor_id="deployment-admin",
        reason="rollback rehearsal",
        changed_at=NOW + timedelta(seconds=3),
    )
    assert disabled.deployment_status == AdapterDeploymentStatus.DISABLED
    assert enabled.deployment_status == AdapterDeploymentStatus.ENABLED
    assert enabled.rollback_target_version == "1.1.0"
    rolled_back = _resolve(
        registry,
        old,
        lock_id="lock-after-rollback",
        request_id="request-after-rollback",
    )
    assert rolled_back.adapter_version == "1.0.0"
    registry.close()


def test_invalid_lifecycle_transition_fails_closed() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, _ = _registry(fixture)
    with pytest.raises(AdapterLifecycleError, match="newer"):
        registry.supersede(
            adapter_id=fixture.descriptor.adapter_id,
            adapter_version=fixture.descriptor.adapter_version,
            superseded_by_version=fixture.descriptor.adapter_version,
            deployment_event_id="invalid",
            actor_id="admin",
            reason="invalid",
            changed_at=NOW,
        )
    registry.close()


def test_size_limits_and_adapter_exception_return_typed_failure_receipts() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    base_impl = fixture.implementation_factory()
    assert base_impl is not None
    oversized = _derived_registration(
        fixture,
        version="1.1.0",
        function=lambda adapter_input: (
            tuple(base_impl.normalize(adapter_input)) * 3
        ),
    )
    registry, _ = _registry(
        oversized,
        policy=AdapterExecutionPolicy(
            max_input_bytes=100,
            max_output_candidates=2,
        ),
    )
    _enable(registry, oversized)
    lock = _resolve(registry, oversized)
    output_result = registry.execute(
        execution_receipt_id="oversized-output",
        adapter_input=_input(oversized, lock),
    )
    assert output_result.receipt.status == AdapterExecutionStatus.OVERSIZED_OUTPUT
    assert (
        output_result.receipt.quarantine_reason
        == QuarantineReason.OVERSIZED_PAYLOAD
    )

    registry.close()
    exception_registration = _derived_registration(
        fixture,
        version="1.2.0",
        function=lambda _adapter_input: (_ for _ in ()).throw(
            RuntimeError("secret raw payload must not escape")
        ),
    )
    exception_registry, _ = _registry(exception_registration)
    _enable(exception_registry, exception_registration)
    exception_lock = _resolve(exception_registry, exception_registration)
    exception_result = exception_registry.execute(
        execution_receipt_id="adapter-exception",
        adapter_input=_input(exception_registration, exception_lock),
    )
    assert (
        exception_result.receipt.status
        == AdapterExecutionStatus.ADAPTER_EXCEPTION
    )
    assert (
        exception_result.receipt.quarantine_reason
        == QuarantineReason.ADAPTER_EXCEPTION
    )
    assert "secret" not in exception_result.model_dump_json()
    exception_registry.close()


def test_oversized_input_is_rejected_before_adapter_execution() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    registry, _ = _registry(
        fixture,
        policy=AdapterExecutionPolicy(max_input_bytes=8),
    )
    _enable(registry, fixture)
    lock = _resolve(registry, fixture)
    result = registry.execute(
        execution_receipt_id="oversized-input",
        adapter_input=_input(fixture, lock),
    )
    assert result.receipt.status == AdapterExecutionStatus.INPUT_REJECTED
    assert result.receipt.detail_code == "oversized_input"
    assert result.receipt.quarantine_reason == QuarantineReason.OVERSIZED_PAYLOAD
    assert result.candidates == ()
    registry.close()


def test_one_provider_exception_does_not_fail_another_provider() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    failing = _derived_registration(
        fixture,
        adapter_id="failing-adapter",
        provider_id="failing-provider",
        account_id="fixture-account",
        version="1.0.0",
        function=lambda _adapter_input: (_ for _ in ()).throw(
            RuntimeError("isolated")
        ),
    )
    healthy = _derived_registration(
        fixture,
        adapter_id="healthy-adapter",
        provider_id="healthy-provider",
        account_id="fixture-account-2",
        version="1.0.0",
        function=lambda _adapter_input: (),
    )
    registry, _ = _registry(failing, healthy)
    _enable(registry, failing, suffix="failing")
    _enable(registry, healthy, suffix="healthy")
    failing_lock = _resolve(registry, failing)
    healthy_lock = _resolve(
        registry,
        healthy,
        account_id="fixture-account-2",
        lock_id="healthy-lock",
        request_id="healthy-request",
    )

    failed = registry.execute(
        execution_receipt_id="failing-execution",
        adapter_input=_input(failing, failing_lock),
    )
    succeeded = registry.execute(
        execution_receipt_id="healthy-execution",
        adapter_input=_input(
            healthy,
            healthy_lock,
            account_id="fixture-account-2",
            raw_id="raw-healthy",
        ),
    )

    assert failed.receipt.status == AdapterExecutionStatus.ADAPTER_EXCEPTION
    assert succeeded.receipt.status == AdapterExecutionStatus.SUCCEEDED
    assert registry.circuit_state("healthy-provider") == CircuitState.CLOSED
    registry.close()


def test_timeout_opens_only_the_failing_provider_circuit() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    slow = _derived_registration(
        fixture,
        provider_id="slow-provider",
        account_id="fixture-account",
        version="1.0.0",
        function=lambda _adapter_input: (
            time.sleep(0.05) or ()
        ),
    )
    registry, _ = _registry(
        slow,
        policy=AdapterExecutionPolicy(
            timeout_seconds=0.005,
            circuit_failure_threshold=1,
            circuit_recovery_seconds=10,
        ),
    )
    _enable(registry, slow)
    lock = _resolve(registry, slow)
    adapter_input = _input(slow, lock)
    first = registry.execute(
        execution_receipt_id="timeout-1",
        adapter_input=adapter_input,
    )
    second = registry.execute(
        execution_receipt_id="timeout-2",
        adapter_input=adapter_input,
    )
    assert first.receipt.status == AdapterExecutionStatus.TIMED_OUT
    assert first.receipt.quarantine_reason == QuarantineReason.ADAPTER_TIMEOUT
    assert second.receipt.status == AdapterExecutionStatus.CIRCUIT_OPEN
    assert registry.circuit_state("slow-provider") == CircuitState.OPEN
    assert registry.circuit_state("other-provider") == CircuitState.CLOSED
    time.sleep(0.06)
    registry.close()


def test_per_provider_bulkhead_rejects_overlap_without_blocking() -> None:
    fixture = hardware_free_fixture_registration(
        provider_account_ids=("fixture-account",)
    )
    base_impl = fixture.implementation_factory()
    assert base_impl is not None
    entered = threading.Event()
    release = threading.Event()

    def blocking(adapter_input: AdapterInputEnvelope):
        entered.set()
        release.wait(timeout=1)
        return base_impl.normalize(adapter_input)

    blocked = _derived_registration(
        fixture,
        version="1.0.0",
        function=blocking,
    )
    registry, _ = _registry(
        blocked,
        policy=AdapterExecutionPolicy(
            max_provider_concurrency=1,
            timeout_seconds=0.5,
        ),
    )
    _enable(registry, blocked)
    lock = _resolve(registry, blocked)
    adapter_input = _input(blocked, lock)
    first_result: list[object] = []
    worker = threading.Thread(
        target=lambda: first_result.append(
            registry.execute(
                execution_receipt_id="bulkhead-first",
                adapter_input=adapter_input,
            )
        )
    )
    worker.start()
    assert entered.wait(timeout=0.2)
    second = registry.execute(
        execution_receipt_id="bulkhead-second",
        adapter_input=adapter_input,
    )
    release.set()
    worker.join(timeout=1)

    assert second.receipt.status == AdapterExecutionStatus.BULKHEAD_REJECTED
    assert first_result
    assert first_result[0].receipt.status == AdapterExecutionStatus.SUCCEEDED
    registry.close()
