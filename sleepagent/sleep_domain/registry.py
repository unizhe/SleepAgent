"""Controlled Adapter protocol, static registry, and conformance execution.

This module has no Agent, binding, Episode, ingestion, network, or API
dependencies.  Adapter code is deployment-owned and supplied through an
immutable allowlist; there is intentionally no runtime registration/import
surface.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal, Protocol, Sequence, runtime_checkable

from pydantic import Field, model_validator

from sleepagent.sleep_domain.contracts import (
    AdapterCapability,
    AdapterDeploymentEvent,
    AdapterDeploymentStatus,
    AdapterDescriptor,
    AdapterObservationCandidate,
    AdapterResolutionLock,
    AlgorithmVersionValue,
    AvailabilityState,
    CalibrationValue,
    CapabilityDeclaration,
    CapabilitySupport,
    CapabilityVerificationReceipt,
    CapabilityVerificationStatus,
    ConfidenceValue,
    DataMode,
    HeartRatePayload,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    QuarantineReason,
    RawIngressRecord,
    ResolvedAdapterCapability,
    SleepDomainContract,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    SleepDomainRepository,
)


_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class AdapterRegistryError(RuntimeError):
    pass


class AdapterDeniedError(AdapterRegistryError):
    pass


class AdapterLifecycleError(AdapterRegistryError):
    pass


class AdapterVerificationError(AdapterRegistryError):
    pass


class AdapterLockError(AdapterRegistryError):
    pass


class AdapterInputEnvelope(SleepDomainContract):
    """Internal normalization input; raw bytes never enter Adapter output."""

    schema_version: Literal["adapter_input_envelope.v1"] = (
        "adapter_input_envelope.v1"
    )
    data_mode: DataMode
    adapter_resolution_lock_id: str = Field(..., min_length=1)
    environment: str = Field(..., min_length=1)
    raw_record: RawIngressRecord
    raw_payload: bytes = Field(..., min_length=1)

    @model_validator(mode="after")
    def raw_bytes_match_metadata(self) -> "AdapterInputEnvelope":
        if self.data_mode != self.raw_record.data_mode:
            raise ValueError("input data_mode does not match raw metadata")
        if len(self.raw_payload) != self.raw_record.payload_size_bytes:
            raise ValueError("raw payload size does not match raw metadata")
        digest = hashlib.sha256(self.raw_payload).hexdigest()
        if digest != self.raw_record.pre_normalization_payload_sha256:
            raise ValueError("raw payload hash does not match raw metadata")
        return self


@runtime_checkable
class AdapterProtocol(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def normalize(
        self,
        adapter_input: AdapterInputEnvelope,
    ) -> Sequence[AdapterObservationCandidate]: ...


@dataclass(frozen=True)
class StaticAdapterRegistration:
    """Deployment-owned code/descriptor pair; never constructed from user input."""

    descriptor: AdapterDescriptor
    implementation_factory: Callable[[], AdapterProtocol] | None = None
    real_device_implementation: bool = False
    effect_policy: "AdapterEffectPolicy" = field(
        default_factory=lambda: AdapterEffectPolicy(),
        init=False,
    )


class AdapterEffectPolicy(SleepDomainContract):
    """Non-escalatable capabilities granted to every Adapter implementation."""

    schema_version: Literal["adapter_effect_policy.v1"] = (
        "adapter_effect_policy.v1"
    )
    can_call_agents: Literal[False] = False
    can_write_external_state: Literal[False] = False
    can_send_raw_payload_to_llm: Literal[False] = False
    can_assign_subject_or_episode: Literal[False] = False


class AdapterExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    IMPLEMENTATION_UNAVAILABLE = "implementation_unavailable"
    INPUT_REJECTED = "input_rejected"
    CIRCUIT_OPEN = "circuit_open"
    BULKHEAD_REJECTED = "bulkhead_rejected"
    TIMED_OUT = "timed_out"
    ADAPTER_EXCEPTION = "adapter_exception"
    INVALID_OUTPUT = "invalid_output"
    OVERSIZED_OUTPUT = "oversized_output"


class AdapterExecutionReceipt(SleepDomainContract):
    schema_version: Literal["adapter_execution_receipt.v1"] = (
        "adapter_execution_receipt.v1"
    )
    execution_receipt_id: str = Field(..., min_length=1)
    data_mode: DataMode
    raw_ingress_record_id: str = Field(..., min_length=1)
    adapter_resolution_lock_id: str = Field(..., min_length=1)
    provider_id: str = Field(..., min_length=1)
    adapter_id: str = Field(..., min_length=1)
    adapter_version: str = Field(..., min_length=1)
    status: AdapterExecutionStatus
    started_at: datetime
    completed_at: datetime
    candidate_count: int = Field(..., ge=0)
    detail_code: str | None = None
    quarantine_reason: QuarantineReason | None = None

    @model_validator(mode="after")
    def receipt_is_coherent(self) -> "AdapterExecutionReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be before started_at")
        if self.status == AdapterExecutionStatus.SUCCEEDED:
            if self.detail_code is not None or self.quarantine_reason is not None:
                raise ValueError("successful execution cannot carry failure detail")
        elif not self.detail_code:
            raise ValueError("failed execution requires detail_code")
        return self


class AdapterExecutionResult(SleepDomainContract):
    schema_version: Literal["adapter_execution_result.v1"] = (
        "adapter_execution_result.v1"
    )
    data_mode: DataMode
    receipt: AdapterExecutionReceipt
    candidates: tuple[AdapterObservationCandidate, ...] = ()

    @model_validator(mode="after")
    def result_matches_receipt(self) -> "AdapterExecutionResult":
        if self.data_mode != self.receipt.data_mode:
            raise ValueError("result data_mode must match receipt")
        if self.receipt.candidate_count != len(self.candidates):
            raise ValueError("receipt candidate_count must match candidates")
        if (
            self.receipt.status != AdapterExecutionStatus.SUCCEEDED
            and self.candidates
        ):
            raise ValueError("failed execution cannot release candidates")
        return self


class AdapterConformanceCheck(SleepDomainContract):
    schema_version: Literal["adapter_conformance_check.v1"] = (
        "adapter_conformance_check.v1"
    )
    check_name: str = Field(..., min_length=1)
    passed: bool
    detail_code: str = Field(..., min_length=1)


class AdapterConformanceReport(SleepDomainContract):
    """Evidence only; a report can never promote a capability to VERIFIED."""

    schema_version: Literal["adapter_conformance_report.v1"] = (
        "adapter_conformance_report.v1"
    )
    data_mode: DataMode
    adapter_id: str = Field(..., min_length=1)
    adapter_version: str = Field(..., min_length=1)
    adapter_artifact_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    configuration_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    checks: tuple[AdapterConformanceCheck, ...]
    passed: bool
    verification_status: Literal[CapabilityVerificationStatus.UNVERIFIED] = (
        CapabilityVerificationStatus.UNVERIFIED
    )

    @model_validator(mode="after")
    def summary_matches_checks(self) -> "AdapterConformanceReport":
        if not self.checks:
            raise ValueError("conformance report requires checks")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("conformance summary must match checks")
        return self


class AdapterConformanceSuite:
    """Hardware-free, deterministic protocol checks with no promotion authority."""

    def run(
        self,
        registration: StaticAdapterRegistration,
        adapter_input: AdapterInputEnvelope,
    ) -> AdapterConformanceReport:
        descriptor = registration.descriptor
        checks: list[AdapterConformanceCheck] = []

        def record(name: str, passed: bool, detail: str) -> None:
            checks.append(
                AdapterConformanceCheck(
                    check_name=name,
                    passed=passed,
                    detail_code=detail,
                )
            )

        try:
            _semver_key(descriptor.adapter_version)
            record("strict_semver", True, "strict_semver")
        except AdapterDeniedError:
            record("strict_semver", False, "invalid_semver")

        implementation: AdapterProtocol | None = None
        if registration.implementation_factory is not None:
            try:
                implementation = registration.implementation_factory()
                matches = (
                    isinstance(implementation, AdapterProtocol)
                    and implementation.descriptor == descriptor
                )
                record(
                    "protocol_and_descriptor",
                    matches,
                    "protocol_descriptor_match"
                    if matches
                    else "protocol_descriptor_mismatch",
                )
            except BaseException:
                record(
                    "protocol_and_descriptor",
                    False,
                    "implementation_factory_exception",
                )
        else:
            record(
                "protocol_and_descriptor",
                False,
                "implementation_unavailable",
            )

        real_claim_statuses = {
            declaration.verification_status
            for declaration in descriptor.capabilities
            if declaration.support == CapabilitySupport.YES
        }
        no_fixture_promotion = (
            registration.real_device_implementation
            or CapabilityVerificationStatus.VERIFIED not in real_claim_statuses
        )
        record(
            "no_fixture_verified_claim",
            no_fixture_promotion,
            "no_fixture_verified_claim"
            if no_fixture_promotion
            else "fixture_claims_real_verification",
        )

        if implementation is not None:
            try:
                first = tuple(implementation.normalize(adapter_input))
                second = tuple(implementation.normalize(adapter_input))
                only_candidates = all(
                    isinstance(item, AdapterObservationCandidate)
                    for item in first
                )
                record(
                    "candidate_only_output",
                    only_candidates,
                    "candidate_only_output"
                    if only_candidates
                    else "non_candidate_output",
                )
                deterministic = (
                    [item.model_dump_json() for item in first]
                    == [item.model_dump_json() for item in second]
                )
                record(
                    "deterministic_output",
                    deterministic,
                    "deterministic_output"
                    if deterministic
                    else "nondeterministic_output",
                )
                serialized = json.dumps(
                    [item.model_dump(mode="json") for item in first],
                    sort_keys=True,
                )
                no_identity = not any(
                    forbidden in serialized
                    for forbidden in (
                        '"subject_id"',
                        '"device_binding_id"',
                        '"binding_version"',
                        '"night_episode_id"',
                    )
                )
                record(
                    "no_subject_binding_episode_identity",
                    no_identity,
                    "identity_absent"
                    if no_identity
                    else "forbidden_identity_present",
                )
                no_raw = (
                    '"raw_payload"' not in serialized
                    and '"data_payload"' not in serialized
                )
                record(
                    "no_legacy_raw_output",
                    no_raw,
                    "legacy_raw_absent"
                    if no_raw
                    else "legacy_raw_present",
                )
            except BaseException:
                record("candidate_only_output", False, "normalization_exception")
                record("deterministic_output", False, "normalization_exception")
                record(
                    "no_subject_binding_episode_identity",
                    False,
                    "normalization_exception",
                )
                record("no_legacy_raw_output", False, "normalization_exception")

        return AdapterConformanceReport(
            data_mode=adapter_input.data_mode,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            adapter_artifact_sha256=descriptor.adapter_artifact_sha256,
            configuration_fingerprint=descriptor.configuration_fingerprint,
            checks=tuple(checks),
            passed=all(check.passed for check in checks),
        )


@dataclass(frozen=True)
class AdapterExecutionPolicy:
    timeout_seconds: float = 2.0
    max_input_bytes: int = 1_048_576
    max_output_candidates: int = 256
    max_output_bytes: int = 1_048_576
    max_provider_concurrency: int = 4
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0

    def __post_init__(self) -> None:
        numeric = (
            self.timeout_seconds,
            self.max_input_bytes,
            self.max_output_candidates,
            self.max_output_bytes,
            self.max_provider_concurrency,
            self.circuit_failure_threshold,
            self.circuit_recovery_seconds,
        )
        if any(value <= 0 for value in numeric):
            raise ValueError("Adapter execution policy limits must be positive")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _ProviderIsolation:
    policy: AdapterExecutionPolicy
    semaphore: threading.BoundedSemaphore = field(init=False)
    executor: ThreadPoolExecutor = field(init=False)
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_probe_active: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(
            self.policy.max_provider_concurrency
        )
        self.executor = ThreadPoolExecutor(
            max_workers=self.policy.max_provider_concurrency,
            thread_name_prefix="sleep-adapter",
        )

    def permits_call(self, now: float) -> bool:
        with self.lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                assert self.opened_at is not None
                if now - self.opened_at < self.policy.circuit_recovery_seconds:
                    return False
                self.state = CircuitState.HALF_OPEN
                self.half_open_probe_active = True
                return True
            if self.half_open_probe_active:
                return False
            self.half_open_probe_active = True
            return True

    def record_success(self) -> None:
        with self.lock:
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.opened_at = None
            self.half_open_probe_active = False

    def record_failure(self, now: float) -> None:
        with self.lock:
            self.consecutive_failures += 1
            self.half_open_probe_active = False
            if (
                self.state == CircuitState.HALF_OPEN
                or self.consecutive_failures
                >= self.policy.circuit_failure_threshold
            ):
                self.state = CircuitState.OPEN
                self.opened_at = now

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class ControlledAdapterRegistry:
    """Immutable allowlist plus persistent lifecycle/verification/version locks."""

    def __init__(
        self,
        *,
        namespace: DomainNamespace,
        repository: SleepDomainRepository,
        allowlist: Sequence[StaticAdapterRegistration],
        authorized_human_reviewers: frozenset[str],
        execution_policy: AdapterExecutionPolicy | None = None,
    ) -> None:
        if not allowlist:
            raise ValueError("static Adapter allowlist cannot be empty")
        self.namespace = namespace
        self.repository = repository
        self.authorized_human_reviewers = authorized_human_reviewers
        self.execution_policy = execution_policy or AdapterExecutionPolicy()
        self._registrations: dict[tuple[str, str], StaticAdapterRegistration] = {}
        self._provider_isolation: dict[str, _ProviderIsolation] = {}
        self._lifecycle_lock = threading.RLock()
        for registration in tuple(allowlist):
            self._validate_static_registration(registration)
            key = (
                registration.descriptor.adapter_id,
                registration.descriptor.adapter_version,
            )
            if key in self._registrations:
                raise ValueError(f"duplicate static Adapter registration: {key}")
            self._registrations[key] = registration
            self.repository.save_adapter_descriptor(
                namespace,
                registration.descriptor,
                created_at=datetime.now(timezone.utc),
            )

    def close(self) -> None:
        for isolation in self._provider_isolation.values():
            isolation.close()

    def deployment_status(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
    ) -> AdapterDeploymentStatus:
        registration = self._registration(adapter_id, adapter_version)
        latest = self.repository.latest_adapter_deployment_event(
            self.namespace,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        )
        return (
            latest.deployment_status
            if latest is not None
            else registration.descriptor.deployment_status
        )

    def enable(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
        deployment_event_id: str,
        actor_id: str,
        reason: str,
        changed_at: datetime,
        rollback_target_version: str | None = None,
    ) -> AdapterDeploymentEvent:
        return self._transition(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            deployment_event_id=deployment_event_id,
            deployment_status=AdapterDeploymentStatus.ENABLED,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at,
            rollback_target_version=rollback_target_version,
        )

    def disable(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
        deployment_event_id: str,
        actor_id: str,
        reason: str,
        changed_at: datetime,
    ) -> AdapterDeploymentEvent:
        return self._transition(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            deployment_event_id=deployment_event_id,
            deployment_status=AdapterDeploymentStatus.DISABLED,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at,
        )

    def supersede(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
        superseded_by_version: str,
        deployment_event_id: str,
        actor_id: str,
        reason: str,
        changed_at: datetime,
    ) -> AdapterDeploymentEvent:
        old = self._registration(adapter_id, adapter_version)
        replacement = self._registration(adapter_id, superseded_by_version)
        if old.descriptor.provider_id != replacement.descriptor.provider_id:
            raise AdapterLifecycleError(
                "superseding Adapter must use the same provider"
            )
        if _semver_key(superseded_by_version) <= _semver_key(adapter_version):
            raise AdapterLifecycleError(
                "superseding Adapter version must be newer"
            )
        return self._transition(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            deployment_event_id=deployment_event_id,
            deployment_status=AdapterDeploymentStatus.SUPERSEDED,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at,
            superseded_by_version=superseded_by_version,
        )

    def rollback(
        self,
        *,
        adapter_id: str,
        from_version: str,
        to_version: str,
        disable_event_id: str,
        enable_event_id: str,
        actor_id: str,
        reason: str,
        changed_at: datetime,
    ) -> tuple[AdapterDeploymentEvent, AdapterDeploymentEvent]:
        self._registration(adapter_id, from_version)
        self._registration(adapter_id, to_version)
        if self.deployment_status(
            adapter_id=adapter_id,
            adapter_version=from_version,
        ) != AdapterDeploymentStatus.ENABLED:
            raise AdapterLifecycleError("rollback source must be enabled")
        disabled = self.disable(
            adapter_id=adapter_id,
            adapter_version=from_version,
            deployment_event_id=disable_event_id,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at,
        )
        enabled = self.enable(
            adapter_id=adapter_id,
            adapter_version=to_version,
            deployment_event_id=enable_event_id,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at,
            rollback_target_version=from_version,
        )
        return disabled, enabled

    def record_verification(
        self,
        receipt: CapabilityVerificationReceipt,
    ) -> CapabilityVerificationReceipt:
        registration = self._registration(
            receipt.adapter_id,
            receipt.adapter_version,
        )
        descriptor = registration.descriptor
        if receipt.data_mode != self.namespace.data_mode:
            raise AdapterVerificationError(
                "verification receipt data_mode does not match registry"
            )
        if receipt.adapter_artifact_sha256 != descriptor.adapter_artifact_sha256:
            raise AdapterVerificationError("Adapter artifact hash mismatch")
        if (
            receipt.configuration_fingerprint
            != descriptor.configuration_fingerprint
        ):
            raise AdapterVerificationError("Adapter configuration hash mismatch")
        declaration = _capability_declaration(
            descriptor,
            receipt.capability,
            receipt.environment,
        )
        if declaration is None or declaration.support != CapabilitySupport.YES:
            raise AdapterVerificationError(
                "capability/environment is not declared as supported"
            )
        if (
            receipt.status == CapabilityVerificationStatus.VERIFIED
            and receipt.reviewed_by_actor_id not in self.authorized_human_reviewers
        ):
            raise AdapterVerificationError(
                "VERIFIED promotion requires an authorized human reviewer"
            )
        if (
            receipt.status == CapabilityVerificationStatus.VERIFIED
            and not registration.real_device_implementation
        ):
            raise AdapterVerificationError(
                "hardware-free or declarative Adapter cannot claim VERIFIED "
                "real-device capability"
            )
        return self.repository.save_capability_verification_receipt(
            self.namespace,
            receipt,
        )

    def resolve(
        self,
        *,
        provider_id: str,
        provider_account_id: str,
        environment: str,
        required_capabilities: Sequence[AdapterCapability],
        adapter_resolution_lock_id: str,
        resolution_request_id: str,
        resolved_at: datetime,
        adapter_id: str | None = None,
        version: str = "latest",
        require_verified: bool = False,
    ) -> AdapterResolutionLock:
        # Production is fail-closed even when a legacy caller omits the old
        # opt-in flag.  Non-production conformance and isolated replay flows
        # may still resolve unverified capabilities explicitly.
        require_verified = require_verified or environment.lower() in {
            "prod",
            "production",
        }
        capabilities = tuple(required_capabilities)
        if not capabilities or len(capabilities) != len(set(capabilities)):
            raise AdapterDeniedError(
                "required capabilities must be non-empty and unique"
            )
        account = self.repository.get_provider_account(
            self.namespace,
            provider_account_id=provider_account_id,
        )
        if (
            account is None
            or account.provider_id != provider_id
            or account.status != "enabled"
        ):
            raise AdapterDeniedError(
                "provider account is absent, disabled, or belongs to another provider"
            )
        candidates: list[StaticAdapterRegistration] = []
        for registration in self._registrations.values():
            descriptor = registration.descriptor
            if descriptor.provider_id != provider_id:
                continue
            if adapter_id is not None and descriptor.adapter_id != adapter_id:
                continue
            if self.namespace.data_mode not in descriptor.supported_data_modes:
                continue
            if (
                descriptor.supported_provider_account_ids
                and provider_account_id
                not in descriptor.supported_provider_account_ids
            ):
                continue
            if (
                descriptor.configuration_fingerprint
                != account.configuration_fingerprint
            ):
                continue
            if self.deployment_status(
                adapter_id=descriptor.adapter_id,
                adapter_version=descriptor.adapter_version,
            ) != AdapterDeploymentStatus.ENABLED:
                continue
            if version != "latest" and descriptor.adapter_version != version:
                continue
            resolved_capabilities = self._resolved_capabilities(
                descriptor,
                capabilities,
                environment,
                require_verified=require_verified,
            )
            if resolved_capabilities is None:
                continue
            candidates.append(registration)
        if version != "latest":
            _semver_key(version)
        if not candidates:
            raise AdapterDeniedError(
                "no enabled allowlisted Adapter satisfies this exact request"
            )
        registration = max(
            candidates,
            key=lambda item: _semver_key(item.descriptor.adapter_version),
        )
        descriptor = registration.descriptor
        resolved_capabilities = self._resolved_capabilities(
            descriptor,
            capabilities,
            environment,
            require_verified=require_verified,
        )
        assert resolved_capabilities is not None
        descriptor_sha256 = hashlib.sha256(
            descriptor.model_dump_json().encode("utf-8")
        ).hexdigest()
        lock = AdapterResolutionLock(
            adapter_resolution_lock_id=adapter_resolution_lock_id,
            data_mode=self.namespace.data_mode,
            provider_id=descriptor.provider_id,
            provider_account_id=provider_account_id,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            contract_version=descriptor.contract_version,
            adapter_artifact_sha256=descriptor.adapter_artifact_sha256,
            configuration_fingerprint=descriptor.configuration_fingerprint,
            environment=environment,
            capabilities=resolved_capabilities,
            descriptor_sha256=descriptor_sha256,
            resolution_request_id=resolution_request_id,
            resolved_at=resolved_at,
        )
        return self.repository.save_adapter_resolution_lock(
            self.namespace,
            lock,
        )

    def load_retained_lock(
        self,
        adapter_resolution_lock_id: str,
    ) -> AdapterResolutionLock:
        lock = self.repository.load_adapter_resolution_lock(
            self.namespace,
            adapter_resolution_lock_id=adapter_resolution_lock_id,
        )
        if lock is None:
            raise AdapterLockError("unknown Adapter resolution lock")
        self._registration(lock.adapter_id, lock.adapter_version)
        return lock

    def retained_reference_count(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
    ) -> int:
        self._registration(adapter_id, adapter_version)
        return self.repository.count_adapter_resolution_references(
            self.namespace,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        )

    def execute(
        self,
        *,
        execution_receipt_id: str,
        adapter_input: AdapterInputEnvelope,
    ) -> AdapterExecutionResult:
        started_at = datetime.now(timezone.utc)
        try:
            lock = self.load_retained_lock(
                adapter_input.adapter_resolution_lock_id
            )
        except AdapterRegistryError:
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.INPUT_REJECTED,
                "unknown_resolution_lock",
                QuarantineReason.UNKNOWN_FORMAT,
            )
        registration = self._registration(lock.adapter_id, lock.adapter_version)
        if not self._input_matches_lock(adapter_input, lock):
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.INPUT_REJECTED,
                "input_lock_mismatch",
                QuarantineReason.UNKNOWN_FORMAT,
                lock=lock,
            )
        if len(adapter_input.raw_payload) > self.execution_policy.max_input_bytes:
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.INPUT_REJECTED,
                "oversized_input",
                QuarantineReason.OVERSIZED_PAYLOAD,
                lock=lock,
            )
        if registration.implementation_factory is None:
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.IMPLEMENTATION_UNAVAILABLE,
                "implementation_unavailable",
                None,
                lock=lock,
            )
        isolation = self._provider_isolation.get(lock.provider_id)
        if isolation is None:
            with self._lifecycle_lock:
                isolation = self._provider_isolation.get(lock.provider_id)
                if isolation is None:
                    isolation = _ProviderIsolation(self.execution_policy)
                    self._provider_isolation[lock.provider_id] = isolation
        monotonic_now = time.monotonic()
        if not isolation.permits_call(monotonic_now):
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.CIRCUIT_OPEN,
                "provider_circuit_open",
                None,
                lock=lock,
            )
        if not isolation.semaphore.acquire(blocking=False):
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.BULKHEAD_REJECTED,
                "provider_bulkhead_full",
                None,
                lock=lock,
            )
        future = isolation.executor.submit(
            _invoke_static_adapter,
            registration,
            adapter_input,
        )
        release_in_finally = True
        try:
            raw_output = future.result(timeout=self.execution_policy.timeout_seconds)
        except TimeoutError:
            release_in_finally = False
            future.cancel()
            future.add_done_callback(
                lambda _future: isolation.semaphore.release()
            )
            isolation.record_failure(time.monotonic())
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.TIMED_OUT,
                "adapter_timeout",
                QuarantineReason.ADAPTER_TIMEOUT,
                lock=lock,
            )
        except BaseException:
            isolation.record_failure(time.monotonic())
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                AdapterExecutionStatus.ADAPTER_EXCEPTION,
                "adapter_exception",
                QuarantineReason.ADAPTER_EXCEPTION,
                lock=lock,
            )
        finally:
            if release_in_finally:
                isolation.semaphore.release()
        validated = self._validate_output(raw_output, lock, adapter_input)
        if isinstance(validated, str):
            isolation.record_failure(time.monotonic())
            status = (
                AdapterExecutionStatus.OVERSIZED_OUTPUT
                if validated.startswith("oversized")
                else AdapterExecutionStatus.INVALID_OUTPUT
            )
            quarantine = (
                QuarantineReason.OVERSIZED_PAYLOAD
                if status == AdapterExecutionStatus.OVERSIZED_OUTPUT
                else QuarantineReason.UNKNOWN_FORMAT
            )
            return self._failure(
                execution_receipt_id,
                adapter_input,
                started_at,
                status,
                validated,
                quarantine,
                lock=lock,
            )
        isolation.record_success()
        completed_at = datetime.now(timezone.utc)
        receipt = AdapterExecutionReceipt(
            execution_receipt_id=execution_receipt_id,
            data_mode=lock.data_mode,
            raw_ingress_record_id=adapter_input.raw_record.raw_ingress_record_id,
            adapter_resolution_lock_id=lock.adapter_resolution_lock_id,
            provider_id=lock.provider_id,
            adapter_id=lock.adapter_id,
            adapter_version=lock.adapter_version,
            status=AdapterExecutionStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=completed_at,
            candidate_count=len(validated),
        )
        return AdapterExecutionResult(
            data_mode=lock.data_mode,
            receipt=receipt,
            candidates=validated,
        )

    def circuit_state(self, provider_id: str) -> CircuitState:
        isolation = self._provider_isolation.get(provider_id)
        return isolation.state if isolation is not None else CircuitState.CLOSED

    def _transition(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
        deployment_event_id: str,
        deployment_status: AdapterDeploymentStatus,
        actor_id: str,
        reason: str,
        changed_at: datetime,
        superseded_by_version: str | None = None,
        rollback_target_version: str | None = None,
    ) -> AdapterDeploymentEvent:
        with self._lifecycle_lock:
            self._registration(adapter_id, adapter_version)
            previous = self.deployment_status(
                adapter_id=adapter_id,
                adapter_version=adapter_version,
            )
            allowed = {
                AdapterDeploymentStatus.REGISTERED: {
                    AdapterDeploymentStatus.ENABLED,
                    AdapterDeploymentStatus.DISABLED,
                },
                AdapterDeploymentStatus.ENABLED: {
                    AdapterDeploymentStatus.DISABLED,
                    AdapterDeploymentStatus.SUPERSEDED,
                },
                AdapterDeploymentStatus.DISABLED: {
                    AdapterDeploymentStatus.ENABLED,
                    AdapterDeploymentStatus.SUPERSEDED,
                },
                AdapterDeploymentStatus.SUPERSEDED: {
                    AdapterDeploymentStatus.ENABLED,
                },
            }
            if deployment_status not in allowed[previous]:
                raise AdapterLifecycleError(
                    f"invalid Adapter deployment transition: "
                    f"{previous.value} -> {deployment_status.value}"
                )
            event = AdapterDeploymentEvent(
                deployment_event_id=deployment_event_id,
                data_mode=self.namespace.data_mode,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                previous_status=previous,
                deployment_status=deployment_status,
                changed_by_actor_id=actor_id,
                change_reason=reason,
                changed_at=changed_at,
                superseded_by_version=superseded_by_version,
                rollback_target_version=rollback_target_version,
            )
            return self.repository.append_adapter_deployment_event(
                self.namespace,
                event,
            )

    def _resolved_capabilities(
        self,
        descriptor: AdapterDescriptor,
        capabilities: tuple[AdapterCapability, ...],
        environment: str,
        *,
        require_verified: bool,
    ) -> tuple[ResolvedAdapterCapability, ...] | None:
        resolved: list[ResolvedAdapterCapability] = []
        for capability in capabilities:
            declaration = _capability_declaration(
                descriptor,
                capability,
                environment,
            )
            if declaration is None or declaration.support != CapabilitySupport.YES:
                return None
            receipt = self.repository.latest_capability_verification_receipt(
                self.namespace,
                adapter_id=descriptor.adapter_id,
                adapter_version=descriptor.adapter_version,
                capability=capability.value,
                environment=environment,
            )
            status = (
                receipt.status
                if receipt is not None
                else declaration.verification_status
            )
            if status == CapabilityVerificationStatus.FAILED:
                return None
            if require_verified and status != CapabilityVerificationStatus.VERIFIED:
                return None
            resolved.append(
                ResolvedAdapterCapability(
                    capability=capability,
                    verification_status=status,
                )
            )
        return tuple(resolved)

    def _validate_static_registration(
        self,
        registration: StaticAdapterRegistration,
    ) -> None:
        descriptor = registration.descriptor
        _semver_key(descriptor.adapter_version)
        if descriptor.deployment_status != AdapterDeploymentStatus.REGISTERED:
            raise ValueError(
                "static descriptor must start with REGISTERED deployment status"
            )
        if not descriptor.supported_provider_account_ids:
            raise ValueError(
                "static descriptor requires an explicit provider-account allowlist"
            )
        if registration.implementation_factory is not None:
            implementation = registration.implementation_factory()
            if not isinstance(implementation, AdapterProtocol):
                raise TypeError("allowlisted implementation violates AdapterProtocol")
            if implementation.descriptor != descriptor:
                raise ValueError(
                    "allowlisted implementation descriptor does not match registration"
                )

    def _registration(
        self,
        adapter_id: str,
        adapter_version: str,
    ) -> StaticAdapterRegistration:
        try:
            return self._registrations[(adapter_id, adapter_version)]
        except KeyError as exc:
            raise AdapterDeniedError(
                "Adapter id/version is not in the static allowlist"
            ) from exc

    def _input_matches_lock(
        self,
        adapter_input: AdapterInputEnvelope,
        lock: AdapterResolutionLock,
    ) -> bool:
        raw = adapter_input.raw_record
        return (
            adapter_input.environment == lock.environment
            and raw.data_mode == lock.data_mode
            and raw.provider_id == lock.provider_id
            and raw.provider_account_id == lock.provider_account_id
        )

    def _validate_output(
        self,
        raw_output: object,
        lock: AdapterResolutionLock,
        adapter_input: AdapterInputEnvelope,
    ) -> tuple[AdapterObservationCandidate, ...] | str:
        if isinstance(raw_output, (str, bytes, bytearray)) or not isinstance(
            raw_output,
            Sequence,
        ):
            return "output_not_candidate_sequence"
        if len(raw_output) > self.execution_policy.max_output_candidates:
            return "oversized_output_candidate_count"
        candidates: list[AdapterObservationCandidate] = []
        output_size = 0
        for item in raw_output:
            if not isinstance(item, AdapterObservationCandidate):
                return "output_item_not_adapter_candidate"
            candidate = AdapterObservationCandidate.model_validate(
                item.model_dump()
            )
            if (
                candidate.data_mode != lock.data_mode
                or candidate.provider_id != lock.provider_id
                or candidate.provider_account_id != lock.provider_account_id
                or candidate.provenance.adapter_id != lock.adapter_id
                or candidate.provenance.adapter_version != lock.adapter_version
                or candidate.provenance.raw_ingress_record_id
                != adapter_input.raw_record.raw_ingress_record_id
                or candidate.provenance.raw_payload_sha256
                != adapter_input.raw_record.pre_normalization_payload_sha256
            ):
                return "candidate_provenance_lock_mismatch"
            serialized = candidate.model_dump_json()
            if '"raw_payload"' in serialized or '"data_payload"' in serialized:
                return "candidate_contains_legacy_payload"
            output_size += len(serialized.encode("utf-8"))
            if output_size > self.execution_policy.max_output_bytes:
                return "oversized_output_bytes"
            candidates.append(candidate)
        return tuple(candidates)

    def _failure(
        self,
        execution_receipt_id: str,
        adapter_input: AdapterInputEnvelope,
        started_at: datetime,
        status: AdapterExecutionStatus,
        detail_code: str,
        quarantine_reason: QuarantineReason | None,
        *,
        lock: AdapterResolutionLock | None = None,
    ) -> AdapterExecutionResult:
        raw = adapter_input.raw_record
        receipt = AdapterExecutionReceipt(
            execution_receipt_id=execution_receipt_id,
            data_mode=raw.data_mode,
            raw_ingress_record_id=raw.raw_ingress_record_id,
            adapter_resolution_lock_id=adapter_input.adapter_resolution_lock_id,
            provider_id=lock.provider_id if lock else raw.provider_id,
            adapter_id=lock.adapter_id if lock else "unknown_allowlisted_adapter",
            adapter_version=lock.adapter_version if lock else "unknown",
            status=status,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            candidate_count=0,
            detail_code=detail_code,
            quarantine_reason=quarantine_reason,
        )
        return AdapterExecutionResult(data_mode=raw.data_mode, receipt=receipt)


def _invoke_static_adapter(
    registration: StaticAdapterRegistration,
    adapter_input: AdapterInputEnvelope,
) -> Sequence[AdapterObservationCandidate]:
    assert registration.implementation_factory is not None
    implementation = registration.implementation_factory()
    if implementation.descriptor != registration.descriptor:
        raise AdapterDeniedError("Adapter descriptor changed after allowlisting")
    return implementation.normalize(adapter_input)


def _semver_key(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise AdapterDeniedError(
            "Adapter version must be strict MAJOR.MINOR.PATCH SemVer"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _capability_declaration(
    descriptor: AdapterDescriptor,
    capability: AdapterCapability,
    environment: str,
) -> CapabilityDeclaration | None:
    return next(
        (
            declaration
            for declaration in descriptor.capabilities
            if declaration.capability == capability
            and declaration.environment == environment
        ),
        None,
    )


def perceptor_v1_descriptor(
    *,
    configuration_fingerprint: str,
    adapter_artifact_sha256: str,
    provider_account_ids: Sequence[str],
    environments: Sequence[str] = ("production",),
) -> AdapterDescriptor:
    if not provider_account_ids or len(provider_account_ids) != len(
        set(provider_account_ids)
    ):
        raise ValueError("Perceptor provider accounts must be non-empty and unique")
    if not environments or len(environments) != len(set(environments)):
        raise ValueError("Perceptor environments must be non-empty and unique")
    supported = (
        AdapterCapability.REALTIME_VITALS,
        AdapterCapability.VITAL_PUSH,
        AdapterCapability.BED_PRESENCE,
        AdapterCapability.MOVEMENT,
        AdapterCapability.ALERTS,
        AdapterCapability.SLEEP_REPORT,
        AdapterCapability.SLEEP_STAGES,
        AdapterCapability.HISTORICAL_RATE_SAMPLES,
    )
    declarations = [
        CapabilityDeclaration(
            capability=capability,
            support=CapabilitySupport.YES,
            environment=environment,
            verification_status=CapabilityVerificationStatus.PENDING,
        )
        for environment in environments
        for capability in supported
    ]
    declarations.extend(
        CapabilityDeclaration(
            capability=AdapterCapability.RAW_RESP_WAVEFORM,
            support=CapabilitySupport.NO,
            environment=environment,
            verification_status=CapabilityVerificationStatus.UNVERIFIED,
        )
        for environment in environments
    )
    return AdapterDescriptor(
        adapter_id="perceptor-v1",
        provider_id="perceptor",
        adapter_version="1.0.0",
        contract_version="adapter_protocol.v1",
        supported_data_modes=frozenset({DataMode.LIVE, DataMode.REPLAY}),
        supported_device_types=("millimeter_wave_radar",),
        supported_provider_account_ids=tuple(provider_account_ids),
        capabilities=tuple(declarations),
        required_credential_names=(
            "PERCEPTOR_APP_KEY",
            "PERCEPTOR_APP_SECRET",
        ),
        required_configuration_names=("PERCEPTOR_BASE_URL",),
        adapter_artifact_sha256=adapter_artifact_sha256,
        configuration_fingerprint=configuration_fingerprint,
        accepted_input_variants=(
            "perceptor_push_object_data.v1",
            "perceptor_push_escaped_json_data.v1",
            "perceptor_sleep_report_pull.v1",
            "perceptor_history_pull.v1",
            "perceptor_realtime_fallback_pull.v1",
        ),
        known_limitations=(
            "local_push_normalization_only_no_network_calls",
            "local_push_and_pull_normalization_only_no_network_calls",
            "push_signing_profile_pending_real_confirmation",
            "pull_response_profile_pending_real_confirmation",
            "alert_semantics_require_real_evidence",
            "respiratory_rate_is_not_a_raw_waveform",
        ),
        output_observation_schema_versions=("adapter_observation_candidate.v1",),
        deployment_status=AdapterDeploymentStatus.REGISTERED,
    )


class _FixturePayload(SleepDomainContract):
    schema_version: Literal["hardware_free_fixture_payload.v1"] = (
        "hardware_free_fixture_payload.v1"
    )
    device_name: str = Field(..., min_length=1)
    heart_rate: float = Field(..., gt=0, le=300)


class HardwareFreeFixtureAdapter:
    """Deterministic no-I/O Adapter used only by the conformance suite."""

    def __init__(self, descriptor: AdapterDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def normalize(
        self,
        adapter_input: AdapterInputEnvelope,
    ) -> Sequence[AdapterObservationCandidate]:
        parsed = json.loads(adapter_input.raw_payload.decode("utf-8"))
        payload = _FixturePayload.model_validate(parsed)
        raw = adapter_input.raw_record
        if raw.measurement_at is None:
            raise ValueError("fixture requires explicit measurement_at")
        identity = ProviderDeviceIdentity(
            provider_device_name=payload.device_name
        )
        candidate_id = hashlib.sha256(
            (
                f"{raw.raw_ingress_record_id}|heart_rate|"
                f"{self.descriptor.adapter_version}"
            ).encode("utf-8")
        ).hexdigest()
        return (
            AdapterObservationCandidate(
                candidate_id=f"fixture:{candidate_id}",
                data_mode=raw.data_mode,
                observation_type=ObservationType.HEART_RATE,
                payload=HeartRatePayload(value=payload.heart_rate),
                source_kind=SourceKind.DEVICE_MEASURED,
                provider_id=raw.provider_id,
                provider_account_id=raw.provider_account_id,
                provider_device=identity,
                request_signed_at=raw.request_signed_at,
                measurement_at=raw.measurement_at,
                event_occurred_at=raw.event_occurred_at,
                received_at=raw.received_at,
                timezone_status=TimezoneStatus.KNOWN,
                quality=ObservationQuality(
                    missing_state=MissingState.PRESENT,
                    confidence=ConfidenceValue(
                        state=AvailabilityState.NOT_PROVIDED
                    ),
                    algorithm_version=AlgorithmVersionValue(
                        state=AvailabilityState.NOT_PROVIDED
                    ),
                    calibration=CalibrationValue(
                        state=AvailabilityState.NOT_PROVIDED
                    ),
                    processing_steps=("hardware_free_fixture_parse.v1",),
                    limitations=("not_real_device_evidence",),
                ),
                provenance=ObservationProvenance(
                    provider_id=raw.provider_id,
                    provider_account_id=raw.provider_account_id,
                    adapter_id=self.descriptor.adapter_id,
                    adapter_version=self.descriptor.adapter_version,
                    raw_ingress_record_id=raw.raw_ingress_record_id,
                    raw_payload_sha256=raw.pre_normalization_payload_sha256,
                    producer_name="hardware_free_fixture_adapter",
                ),
                source_key=f"fixture:{raw.raw_ingress_record_id}:heart_rate",
                idempotency_key=(
                    f"fixture:{raw.idempotency_identity}:heart_rate:"
                    f"{self.descriptor.adapter_version}"
                ),
            ),
        )


def hardware_free_fixture_registration(
    *,
    provider_account_ids: Sequence[str],
    environment: str = "test",
) -> StaticAdapterRegistration:
    if not provider_account_ids or len(provider_account_ids) != len(
        set(provider_account_ids)
    ):
        raise ValueError("fixture provider accounts must be non-empty and unique")
    artifact_hash = hashlib.sha256(
        b"sleepagent.hardware_free_fixture_adapter.v1"
    ).hexdigest()
    config_hash = hashlib.sha256(
        b"sleepagent.hardware_free_fixture_config.v1"
    ).hexdigest()
    descriptor = AdapterDescriptor(
        adapter_id="hardware-free-fixture",
        provider_id="fixture-reference",
        adapter_version="1.0.0",
        contract_version="adapter_protocol.v1",
        supported_data_modes=frozenset({DataMode.REPLAY}),
        supported_device_types=("synthetic_contract_fixture",),
        supported_provider_account_ids=tuple(provider_account_ids),
        capabilities=(
            CapabilityDeclaration(
                capability=AdapterCapability.REALTIME_VITALS,
                support=CapabilitySupport.YES,
                environment=environment,
                verification_status=CapabilityVerificationStatus.UNVERIFIED,
            ),
        ),
        adapter_artifact_sha256=artifact_hash,
        configuration_fingerprint=config_hash,
        accepted_input_variants=("hardware_free_fixture_payload.v1",),
        known_limitations=(
            "conformance_only",
            "not_real_hardware",
            "cannot_support_real_device_verification",
        ),
        output_observation_schema_versions=("adapter_observation_candidate.v1",),
        deployment_status=AdapterDeploymentStatus.REGISTERED,
    )
    return StaticAdapterRegistration(
        descriptor=descriptor,
        implementation_factory=lambda: HardwareFreeFixtureAdapter(descriptor),
        real_device_implementation=False,
    )


def perceptor_v1_registration(
    *,
    configuration_fingerprint: str,
    adapter_artifact_sha256: str,
    provider_account_ids: Sequence[str],
    environments: Sequence[str] = ("production",),
) -> StaticAdapterRegistration:
    """Allowlisted local push/pull normalizer; evidence remains PENDING."""

    descriptor = perceptor_v1_descriptor(
        configuration_fingerprint=configuration_fingerprint,
        adapter_artifact_sha256=adapter_artifact_sha256,
        provider_account_ids=provider_account_ids,
        environments=environments,
    )

    def implementation_factory() -> AdapterProtocol:
        from sleepagent.integrations.perceptor.push_adapter import (
            PerceptorPushAdapter,
        )

        return PerceptorPushAdapter(descriptor)

    return StaticAdapterRegistration(
        descriptor=descriptor,
        implementation_factory=implementation_factory,
        real_device_implementation=True,
    )


__all__ = [
    "AdapterConformanceCheck",
    "AdapterConformanceReport",
    "AdapterConformanceSuite",
    "AdapterDeniedError",
    "AdapterExecutionPolicy",
    "AdapterExecutionReceipt",
    "AdapterExecutionResult",
    "AdapterExecutionStatus",
    "AdapterEffectPolicy",
    "AdapterInputEnvelope",
    "AdapterLifecycleError",
    "AdapterLockError",
    "AdapterProtocol",
    "AdapterRegistryError",
    "AdapterVerificationError",
    "CircuitState",
    "ControlledAdapterRegistry",
    "HardwareFreeFixtureAdapter",
    "StaticAdapterRegistration",
    "hardware_free_fixture_registration",
    "perceptor_v1_descriptor",
    "perceptor_v1_registration",
]
