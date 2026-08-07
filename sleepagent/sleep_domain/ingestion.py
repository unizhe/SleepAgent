"""Safe Adapter candidate promotion and authorized quarantine reprocessing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import model_validator

from sleepagent.sleep_domain.contracts import (
    AdapterObservationCandidate,
    BedExitPayload,
    DeviceBinding,
    DeviceBindingStatus,
    NonEmptyStr,
    ProcessingOutcome,
    ProcessingReceipt,
    ProcessingStage,
    QuarantineReason,
    QuarantineReprocessAudit,
    SleepDomainContract,
    SleepObservation,
    SleepStageIntervalPayload,
    TimezoneStatus,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
    MissingIntervalPayload,
)
from sleepagent.sleep_domain.device_binding import (
    AdministrativeAccessPolicy,
    AdministrativeScope,
    namespaced_provider_device_key,
)
from sleepagent.sleep_domain.radar_compat import bind_adapter_candidate
from sleepagent.sleep_domain.repository import DomainNamespace, SleepDomainRepository


class CandidatePromotionStatus(str, Enum):
    PROMOTED = "promoted"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class CandidatePromotionResult:
    status: CandidatePromotionStatus
    candidate_id: str
    observation: SleepObservation | None
    quarantine_id: str | None
    quarantine_reason: QuarantineReason | None
    receipt: ProcessingReceipt


class QuarantineReprocessCommand(SleepDomainContract):
    schema_version: Literal["quarantine_reprocess_command.v1"] = (
        "quarantine_reprocess_command.v1"
    )
    request_id: NonEmptyStr
    quarantine_ids: tuple[NonEmptyStr, ...]
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    requested_at: datetime
    reason: NonEmptyStr

    @model_validator(mode="after")
    def require_unique_targets(self) -> "QuarantineReprocessCommand":
        if not self.quarantine_ids:
            raise ValueError("reprocessing requires at least one quarantine id")
        if len(set(self.quarantine_ids)) != len(self.quarantine_ids):
            raise ValueError("quarantine ids must be unique")
        return self


@dataclass(frozen=True)
class QuarantineReprocessResult:
    request_id: str
    results: tuple[CandidatePromotionResult, ...]

    @property
    def released_count(self) -> int:
        return sum(
            result.status == CandidatePromotionStatus.PROMOTED
            for result in self.results
        )


_UNTRUSTED_TIME_FLAGS = frozenset(
    {
        "clock_skew",
        "clock_invalid",
        "time_untrusted",
        "timezone_unknown",
        "receipt_time_fallback",
        "request_time_fallback",
        "unparseable_time",
    }
)


class CandidatePromotionService:
    def __init__(
        self,
        repository: SleepDomainRepository,
        *,
        access_policy: AdministrativeAccessPolicy,
        processor_id: str = "sleep-domain-binding-service",
        processor_version: str = "1.0.0",
    ) -> None:
        self.repository = repository
        self.access_policy = access_policy
        self.processor_id = processor_id
        self.processor_version = processor_version

    def promote(
        self,
        namespace: DomainNamespace,
        candidate: AdapterObservationCandidate,
        *,
        attempt_id: str,
        actor_id: str,
        occurred_at: datetime,
        cause_receipt_id: str | None = None,
        acquisition_channel: str | None = None,
    ) -> CandidatePromotionResult:
        """Promote only by trustworthy event time and exactly one interval."""

        if candidate.data_mode != namespace.data_mode:
            raise ValueError("candidate data_mode does not match namespace")
        stage = (
            ProcessingStage.REPAIR
            if cause_receipt_id is not None
            else ProcessingStage.BINDING
        )
        attribution_time, reason, detail_code = _trusted_attribution_time(candidate)
        provider_device_key = namespaced_provider_device_key(
            provider_id=candidate.provider_id,
            provider_account_id=candidate.provider_account_id,
            provider_device=candidate.provider_device,
        )
        matching_bindings: tuple[DeviceBinding, ...] = ()
        interval_bindings: tuple[DeviceBinding, ...] = ()
        if reason is None and attribution_time is not None:
            matching_bindings = self.repository.matching_device_bindings(
                namespace,
                provider_id=candidate.provider_id,
                provider_account_id=candidate.provider_account_id,
                provider_device_key=provider_device_key,
            )
            if not matching_bindings:
                reason = QuarantineReason.DEVICE_UNBOUND
                detail_code = "namespaced_device_not_bound"
            else:
                interval_bindings = tuple(
                    binding
                    for binding in matching_bindings
                    if binding.status != DeviceBindingStatus.REVOKED
                    and binding.effective_from <= attribution_time
                    and (
                        binding.effective_until is None
                        or attribution_time < binding.effective_until
                    )
                )
                if not interval_bindings:
                    reason = QuarantineReason.BINDING_TIME_OUTSIDE_INTERVAL
                    detail_code = "binding_interval_gap"
                elif len(interval_bindings) > 1:
                    reason = QuarantineReason.DEVICE_BINDING_AMBIGUOUS
                    detail_code = "multiple_binding_intervals"

        if reason is not None:
            receipt = ProcessingReceipt(
                receipt_id=f"binding-receipt:{attempt_id}",
                raw_ingress_record_id=(
                    candidate.provenance.raw_ingress_record_id
                ),
                data_mode=candidate.data_mode,
                stage=stage,
                outcome=ProcessingOutcome.QUARANTINED,
                occurred_at=occurred_at,
                actor_id=actor_id,
                processor_id=self.processor_id,
                processor_version=self.processor_version,
                cause_receipt_id=cause_receipt_id,
                quarantine_reason=reason,
                detail_code=detail_code,
            )
            quarantine_id = f"quarantine:{attempt_id}"
            self.repository.commit_candidate_quarantine(
                namespace,
                candidate=candidate,
                quarantine_id=quarantine_id,
                receipt=receipt,
                detail={
                    "candidate_id": candidate.candidate_id,
                    "provider_device_key": provider_device_key,
                    "attribution_time": (
                        None
                        if attribution_time is None
                        else attribution_time.isoformat()
                    ),
                    "matching_binding_count": len(matching_bindings),
                    "interval_binding_count": len(interval_bindings),
                },
            )
            return CandidatePromotionResult(
                status=CandidatePromotionStatus.QUARANTINED,
                candidate_id=candidate.candidate_id,
                observation=None,
                quarantine_id=quarantine_id,
                quarantine_reason=reason,
                receipt=receipt,
            )

        binding = interval_bindings[0]
        observation = bind_adapter_candidate(
            candidate,
            binding,
            observation_id=(
                f"observation:{candidate.candidate_id}:"
                f"{binding.device_binding_id}:v{binding.binding_version}"
            ),
        )
        receipt = ProcessingReceipt(
            receipt_id=f"binding-receipt:{attempt_id}",
            raw_ingress_record_id=candidate.provenance.raw_ingress_record_id,
            data_mode=candidate.data_mode,
            stage=stage,
            outcome=(
                ProcessingOutcome.RELEASED
                if cause_receipt_id is not None
                else ProcessingOutcome.SUCCEEDED
            ),
            occurred_at=occurred_at,
            actor_id=actor_id,
            processor_id=self.processor_id,
            processor_version=self.processor_version,
            cause_receipt_id=cause_receipt_id,
        )
        fact_slot_key, fact_value_sha256 = observation_fact_identity(candidate)
        commit = self.repository.commit_candidate_observation(
            namespace,
            candidate=candidate,
            observation=observation,
            receipt=receipt,
            committed_at=occurred_at,
            fact_slot_key=fact_slot_key,
            fact_value_sha256=fact_value_sha256,
            acquisition_channel=(
                acquisition_channel
                or candidate.provenance.producer_name
                or "unknown"
            ),
        )
        if commit.observation_id != observation.observation_id:
            retained = self.repository.get_observation(
                namespace,
                observation_id=commit.observation_id,
            )
            if retained is None:
                raise RuntimeError("retained fact observation is missing")
            observation = retained
        return CandidatePromotionResult(
            status=CandidatePromotionStatus.PROMOTED,
            candidate_id=candidate.candidate_id,
            observation=observation,
            quarantine_id=None,
            quarantine_reason=None,
            receipt=receipt,
        )

    def reprocess_quarantine(
        self,
        namespace: DomainNamespace,
        command: QuarantineReprocessCommand,
    ) -> QuarantineReprocessResult:
        """Audit an admin request, then retry each immutable candidate as-is."""

        self.access_policy.require(
            actor_id=command.actor_id,
            authorization_id=command.authorization_id,
            scope=AdministrativeScope.QUARANTINE_REPROCESS,
        )
        audit = QuarantineReprocessAudit(
            request_id=command.request_id,
            data_mode=namespace.data_mode,
            quarantine_ids=command.quarantine_ids,
            actor_id=command.actor_id,
            authorization_id=command.authorization_id,
            requested_at=command.requested_at,
            reason=command.reason,
        )
        self.repository.append_quarantine_reprocess_audit(namespace, audit)
        results: list[CandidatePromotionResult] = []
        for quarantine_id in command.quarantine_ids:
            quarantined = self.repository.get_quarantine(
                namespace,
                quarantine_id=quarantine_id,
            )
            if quarantined is None:
                raise KeyError(f"quarantine record not found: {quarantine_id}")
            candidate_id = quarantined.detail.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError(
                    "quarantine record does not reference an immutable candidate"
                )
            candidate = self.repository.get_candidate(
                namespace,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise ValueError("quarantine candidate is missing")
            results.append(
                self.promote(
                    namespace,
                    candidate,
                    attempt_id=(
                        f"reprocess:{command.request_id}:{quarantine_id}"
                    ),
                    actor_id=command.actor_id,
                    occurred_at=command.requested_at,
                    cause_receipt_id=quarantined.receipt_id,
                )
            )
        return QuarantineReprocessResult(
            request_id=command.request_id,
            results=tuple(results),
        )


def _trusted_attribution_time(
    candidate: AdapterObservationCandidate,
) -> tuple[datetime | None, QuarantineReason | None, str | None]:
    if candidate.timezone_status != TimezoneStatus.KNOWN:
        return None, QuarantineReason.TIMEZONE_UNKNOWN, "time_not_independently_zoned"
    normalized_flags = {
        flag.strip().lower().replace("-", "_")
        for flag in candidate.quality.quality_flags
    }
    if normalized_flags & _UNTRUSTED_TIME_FLAGS:
        return None, QuarantineReason.CLOCK_SKEW, "untrusted_time_quality_flag"
    attribution_time = candidate.measurement_at or candidate.event_occurred_at
    if attribution_time is None:
        return None, QuarantineReason.UNPARSEABLE_TIME, "event_time_missing"
    return attribution_time, None, None


def observation_fact_identity(
    candidate: AdapterObservationCandidate,
) -> tuple[str, str]:
    """Return a transport-independent fact slot and a value content hash."""

    attribution_time = candidate.measurement_at or candidate.event_occurred_at
    if attribution_time is None:
        raise ValueError("fact identity requires an attribution time")
    payload = candidate.payload
    semantic_dimension: dict[str, object] = {}
    if isinstance(payload, SleepStageIntervalPayload):
        semantic_dimension = {
            "interval_start_at": (
                None
                if payload.start_at is None
                else payload.start_at.isoformat()
            ),
            "interval_end_at": (
                None
                if payload.end_at is None
                else payload.end_at.isoformat()
            ),
            "source_start_text": payload.source_start_text,
            "source_end_text": payload.source_end_text,
        }
    elif isinstance(payload, VendorSleepProfileMetricPayload):
        semantic_dimension = {"metric_name": payload.metric_name}
    elif isinstance(payload, MissingIntervalPayload):
        semantic_dimension = {
            "target_observation_type": payload.target_observation_type.value,
            "interval_start_at": (
                None
                if payload.interval_start_at is None
                else payload.interval_start_at.isoformat()
            ),
            "interval_end_at": (
                None
                if payload.interval_end_at is None
                else payload.interval_end_at.isoformat()
            ),
        }
    elif isinstance(payload, VendorAlertPayload):
        semantic_dimension = {
            "vendor_alert_instance_id": payload.vendor_alert_instance_id,
            "alert_code": payload.alert_code,
        }
    elif isinstance(payload, BedExitPayload):
        semantic_dimension = {"event_kind_slot": "bed_exit"}
    slot_material = {
        "provider_id": candidate.provider_id,
        "provider_account_id": candidate.provider_account_id,
        "provider_device": candidate.provider_device.model_dump(
            mode="json",
            exclude_none=True,
        ),
        "observation_type": candidate.observation_type.value,
        "attribution_time": attribution_time.isoformat(),
        "semantic_dimension": semantic_dimension,
    }
    value_material = payload.model_dump(mode="json")
    slot_key = hashlib.sha256(
        json.dumps(
            slot_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    value_hash = hashlib.sha256(
        json.dumps(
            value_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"fact-slot:{slot_key}", value_hash


__all__ = [
    "CandidatePromotionResult",
    "CandidatePromotionService",
    "CandidatePromotionStatus",
    "QuarantineReprocessCommand",
    "QuarantineReprocessResult",
    "observation_fact_identity",
]
