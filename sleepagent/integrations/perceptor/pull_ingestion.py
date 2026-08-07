"""Bounded Perceptor pull and reconciliation into the unified Raw Inbox."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import islice
from typing import Any, Mapping, Protocol, Sequence

from sleepagent.integrations.perceptor.pull_adapter import (
    HISTORY_EVENT,
    PULL_ENVELOPE_SCHEMA,
    REALTIME_FALLBACK_EVENT,
    SLEEP_REPORT_EVENT,
    canonical_content_sha256,
)
from sleepagent.sleep_domain import (
    AdapterCapability,
    ControlledAdapterRegistry,
    DomainNamespace,
    ProviderDeviceIdentity,
    RawIngressRecord,
    SignatureVerificationState,
    SleepDomainRepository,
    namespaced_provider_device_key,
)
from sleepagent.sleep_domain.repository import (
    IntakeResult,
    PullCheckpoint,
    SourceReportVersion,
)


class PerceptorPullError(RuntimeError):
    pass


class PerceptorPullConfigurationError(PerceptorPullError):
    pass


class PerceptorPullClient(Protocol):
    def get_sleep_report(
        self,
        *,
        report_date: date | str,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]: ...

    def get_history_data(
        self,
        *,
        device_names: list[str] | tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
        home_id: str | int | None = None,
    ) -> dict[str, Any]: ...

    def start_realtime(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]: ...

    def get_realtime(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PerceptorPullCompatibilityProfile:
    profile_id: str
    provider_account_id: str
    environment: str
    history_window: timedelta = timedelta(hours=1)
    history_device_batch_size: int = 20
    history_cadence: timedelta = timedelta(seconds=3)
    overlap_window: timedelta = timedelta(minutes=10)
    lateness: timedelta = timedelta(minutes=15)
    realtime_fallback_enabled: bool = False
    capability_status: str = "pending"

    def __post_init__(self) -> None:
        if not self.profile_id or not self.provider_account_id or not self.environment:
            raise PerceptorPullConfigurationError(
                "pull profile, provider account, and environment are required"
            )
        if not timedelta(0) < self.history_window <= timedelta(hours=1):
            raise PerceptorPullConfigurationError(
                "history window must be positive and no longer than one hour"
            )
        if not 1 <= self.history_device_batch_size <= 20:
            raise PerceptorPullConfigurationError(
                "history device batch size must be between 1 and 20"
            )
        if self.history_cadence <= timedelta(0):
            raise PerceptorPullConfigurationError(
                "history cadence must be positive"
            )
        if self.overlap_window < timedelta(0) or self.lateness < timedelta(0):
            raise PerceptorPullConfigurationError(
                "overlap and lateness cannot be negative"
            )
        if self.capability_status != "pending":
            raise PerceptorPullConfigurationError(
                "unconfirmed pull profiles must remain PENDING"
            )


@dataclass(frozen=True)
class PerceptorPullDevice:
    device_name: str
    home_id: str | int
    provider_device_id: str | None = None
    product_id: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.device_name.strip()
            or self.home_id is None
            or str(self.home_id) == ""
        ):
            raise PerceptorPullConfigurationError(
                "pull device requires device_name and home_id"
            )

    def identity_payload(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "device_name": self.device_name,
                "home_id": str(self.home_id),
                "device_id": self.provider_device_id,
                "product_id": self.product_id,
                "project_id": self.project_id,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class PulledSourceReport:
    intake: IntakeResult
    source_report: SourceReportVersion

    @property
    def changed(self) -> bool:
        return self.source_report.created


@dataclass(frozen=True)
class HistoryReconciliationResult:
    intake_results: tuple[IntakeResult, ...]
    checkpoint: PullCheckpoint | None
    window_count: int
    batch_count: int


class PerceptorPullService:
    """Transport orchestrator. Normalization remains in the allowlisted Adapter."""

    def __init__(
        self,
        *,
        namespace: DomainNamespace,
        repository: SleepDomainRepository,
        registry: ControlledAdapterRegistry,
        client: PerceptorPullClient,
        profile: PerceptorPullCompatibilityProfile,
    ) -> None:
        self.namespace = namespace
        self.repository = repository
        self.registry = registry
        self.client = client
        self.profile = profile
        self._adapter_resolution_lock = threading.Lock()

    def pull_sleep_report(
        self,
        device: PerceptorPullDevice,
        *,
        report_date: date,
        fetched_at: datetime,
    ) -> PulledSourceReport:
        if not isinstance(report_date, date) or isinstance(report_date, datetime):
            raise PerceptorPullConfigurationError(
                "sleep report requires a local date, not datetime"
            )
        response = self.client.get_sleep_report(
            device_name=device.device_name,
            home_id=device.home_id,
            report_date=report_date,
        )
        _require_success(response, "getSleepReport")
        content_hash = canonical_content_sha256(response.get("data"))
        envelope = {
            "schema": PULL_ENVELOPE_SCHEMA,
            "event_type": SLEEP_REPORT_EVENT,
            "compatibility_profile_id": self.profile.profile_id,
            "request": {
                **device.identity_payload(),
                "local_report_date": report_date.isoformat(),
            },
            "response": response,
        }
        identity = (
            f"perceptor-source-report.v1:{device.device_name}:"
            f"{device.home_id}:{report_date.isoformat()}:{content_hash}"
        )
        intake = self._intake(
            envelope,
            identity=identity,
            event_type=SLEEP_REPORT_EVENT,
            capability=AdapterCapability.SLEEP_REPORT,
            received_at=fetched_at,
        )
        provider_device_key = namespaced_provider_device_key(
            provider_id="perceptor",
            provider_account_id=self.profile.provider_account_id,
            provider_device=ProviderDeviceIdentity(
                provider_device_id=device.provider_device_id,
                provider_device_name=device.device_name,
                product_id=device.product_id,
                home_id=str(device.home_id),
                project_id=device.project_id,
            ),
        )
        version_id = "source-report:" + hashlib.sha256(
            (
                f"{self.namespace.namespace_id}|{provider_device_key}|"
                f"{report_date.isoformat()}|{content_hash}"
            ).encode("utf-8")
        ).hexdigest()
        source_report = self.repository.register_source_report_version(
            self.namespace,
            source_report_version_id=version_id,
            provider_id="perceptor",
            provider_account_id=self.profile.provider_account_id,
            provider_device_key=provider_device_key,
            local_report_date=report_date,
            content_sha256=content_hash,
            raw_ingress_record_id=intake.raw_ingress_record_id,
            is_empty=response.get("data") in (None, {}, []),
            fetched_at=fetched_at,
        )
        return PulledSourceReport(intake=intake, source_report=source_report)

    def reconcile_history(
        self,
        devices: Sequence[PerceptorPullDevice],
        *,
        start_at: datetime,
        end_at: datetime,
        stream_key: str,
        reconciled_at: datetime,
    ) -> HistoryReconciliationResult:
        if not devices:
            raise PerceptorPullConfigurationError(
                "history reconciliation requires at least one device"
            )
        if not stream_key.strip():
            raise PerceptorPullConfigurationError(
                "history reconciliation requires a non-empty stream_key"
            )
        _require_aware_interval(start_at, end_at)
        home_ids = {str(device.home_id) for device in devices}
        if len(home_ids) != 1:
            raise PerceptorPullConfigurationError(
                "one history reconciliation stream must use one home_id"
            )
        if len({device.device_name for device in devices}) != len(devices):
            raise PerceptorPullConfigurationError(
                "history devices must be unique"
            )
        checkpoint = self.repository.get_pull_checkpoint(
            self.namespace,
            provider_id="perceptor",
            provider_account_id=self.profile.provider_account_id,
            stream_key=stream_key,
        )
        effective_start = start_at
        if checkpoint is not None:
            replay_from = checkpoint.lateness_watermark_at - self.profile.overlap_window
            effective_start = max(start_at, replay_from)
        intakes: list[IntakeResult] = []
        window_count = 0
        batch_count = 0
        current_checkpoint = checkpoint
        for window_start, window_end in partition_history_windows(
            effective_start,
            end_at,
            max_window=self.profile.history_window,
        ):
            window_count += 1
            for batch in partition_device_batches(
                devices,
                max_devices=self.profile.history_device_batch_size,
            ):
                batch_count += 1
                response = self.client.get_history_data(
                    device_names=[device.device_name for device in batch],
                    home_id=batch[0].home_id,
                    start_at=window_start,
                    end_at=window_end,
                )
                _require_success(response, "getHistoryData")
                content_hash = canonical_content_sha256(response.get("data"))
                envelope = {
                    "schema": PULL_ENVELOPE_SCHEMA,
                    "event_type": HISTORY_EVENT,
                    "compatibility_profile_id": self.profile.profile_id,
                    "cadence_seconds": self.profile.history_cadence.total_seconds(),
                    "request": {
                        "device_names": [
                            device.device_name for device in batch
                        ],
                        "devices": [
                            device.identity_payload() for device in batch
                        ],
                        "home_id": str(batch[0].home_id),
                        "start_at": window_start.isoformat(),
                        "end_at": window_end.isoformat(),
                    },
                    "response": response,
                }
                identity = (
                    f"perceptor-history.v1:{stream_key}:"
                    f"{window_start.isoformat()}:{window_end.isoformat()}:"
                    f"{','.join(device.device_name for device in batch)}:"
                    f"{content_hash}"
                )
                intakes.append(
                    self._intake(
                        envelope,
                        identity=identity,
                        event_type=HISTORY_EVENT,
                        capability=AdapterCapability.HISTORICAL_RATE_SAMPLES,
                        received_at=reconciled_at,
                    )
                )
                _validate_history_series_lengths(response)
            previous_cursor = (
                current_checkpoint.cursor_at
                if current_checkpoint is not None
                else window_start
            )
            next_cursor = max(previous_cursor, window_end)
            checkpoint_id = "pull-checkpoint:" + hashlib.sha256(
                (
                    f"{self.namespace.namespace_id}|perceptor|"
                    f"{self.profile.provider_account_id}|{stream_key}"
                ).encode("utf-8")
            ).hexdigest()
            current_checkpoint = self.repository.advance_pull_checkpoint(
                self.namespace,
                checkpoint_id=checkpoint_id,
                provider_id="perceptor",
                provider_account_id=self.profile.provider_account_id,
                stream_key=stream_key,
                cursor_at=next_cursor,
                lateness_watermark_at=next_cursor - self.profile.lateness,
                expected_cas_version=(
                    None
                    if current_checkpoint is None
                    else current_checkpoint.cas_version
                ),
                updated_at=reconciled_at,
            )
        return HistoryReconciliationResult(
            intake_results=tuple(intakes),
            checkpoint=current_checkpoint,
            window_count=window_count,
            batch_count=batch_count,
        )

    def run_realtime_fallback(
        self,
        device: PerceptorPullDevice,
        *,
        gap_key: str,
        fetched_at: datetime,
    ) -> IntakeResult:
        if not self.profile.realtime_fallback_enabled:
            raise PerceptorPullConfigurationError(
                "realtime fallback is disabled for this profile"
            )
        if not gap_key.strip():
            raise PerceptorPullConfigurationError(
                "realtime fallback requires a non-empty gap_key"
            )
        start_response = self.client.start_realtime(
            device_name=device.device_name,
            home_id=device.home_id,
        )
        _require_success(start_response, "start")
        response = self.client.get_realtime(
            device_name=device.device_name,
            home_id=device.home_id,
        )
        _require_success(response, "getRealTimes")
        content_hash = canonical_content_sha256(response.get("data"))
        envelope = {
            "schema": PULL_ENVELOPE_SCHEMA,
            "event_type": REALTIME_FALLBACK_EVENT,
            "compatibility_profile_id": self.profile.profile_id,
            "request": {
                **device.identity_payload(),
                "gap_key": gap_key,
            },
            "response": response,
        }
        return self._intake(
            envelope,
            identity=(
                f"perceptor-realtime-fallback.v1:{gap_key}:"
                f"{device.device_name}:{content_hash}"
            ),
            event_type=REALTIME_FALLBACK_EVENT,
            capability=AdapterCapability.REALTIME_VITALS,
            received_at=fetched_at,
        )

    def _intake(
        self,
        envelope: Mapping[str, Any],
        *,
        identity: str,
        event_type: str,
        capability: AdapterCapability,
        received_at: datetime,
    ) -> IntakeResult:
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise PerceptorPullConfigurationError("received_at must be aware")
        raw_payload = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_hash = hashlib.sha256(raw_payload).hexdigest()
        raw_id = "perceptor-pull:" + hashlib.sha256(
            (
                f"{self.namespace.namespace_id}|"
                f"{self.profile.provider_account_id}|{identity}"
            ).encode("utf-8")
        ).hexdigest()
        lock = self._resolve_adapter_lock(capability, received_at=received_at)
        record = RawIngressRecord(
            raw_ingress_record_id=raw_id,
            data_mode=self.namespace.data_mode,
            provider_id="perceptor",
            provider_account_id=self.profile.provider_account_id,
            event_type=event_type,
            message_id=f"pull:{payload_hash}",
            request_signed_at=None,
            measurement_at=None,
            event_occurred_at=None,
            received_at=received_at,
            signature_profile=None,
            signature_verification=SignatureVerificationState.NOT_PROVIDED,
            idempotency_identity=identity,
            idempotency_version="perceptor_pull.v1",
            pre_normalization_payload_sha256=payload_hash,
            encrypted_payload_reference=f"db:sleep_domain_raw_inbox:{raw_id}",
            content_type="application/json",
            payload_size_bytes=len(raw_payload),
            retention_deadline=(
                received_at + self.repository.raw_payload_policy.retention_period
            ),
        )
        return self.repository.intake_raw(
            self.namespace,
            record,
            raw_payload=raw_payload,
            work_id=f"normalize:{raw_id}",
            work_generation=1,
            work_json={
                "adapter_resolution_lock_id": lock.adapter_resolution_lock_id,
                "compatibility_profile_id": self.profile.profile_id,
                "environment": self.profile.environment,
                "ingress_channel": (
                    "realtime_fallback"
                    if event_type == REALTIME_FALLBACK_EVENT
                    else "pull"
                ),
            },
            processing_intent_id=f"processing-intent:{raw_id}",
            processing_intent_json={
                "event_type": "RAW_ACCEPTED",
                "source_event_type": event_type,
            },
            created_at=received_at,
        )

    def _resolve_adapter_lock(
        self,
        capability: AdapterCapability,
        *,
        received_at: datetime,
    ) -> Any:
        capability_key = capability.value
        lock_id = (
            f"perceptor-pull-lock:{self.profile.profile_id}:{capability_key}"
        )
        with self._adapter_resolution_lock:
            retained = self.repository.load_adapter_resolution_lock(
                self.namespace,
                adapter_resolution_lock_id=lock_id,
            )
            if retained is not None:
                return retained
            return self.registry.resolve(
                provider_id="perceptor",
                provider_account_id=self.profile.provider_account_id,
                environment=self.profile.environment,
                required_capabilities=(capability,),
                adapter_resolution_lock_id=lock_id,
                resolution_request_id=(
                    f"perceptor-pull-resolution:{self.profile.profile_id}:"
                    f"{capability_key}"
                ),
                resolved_at=received_at,
                adapter_id="perceptor-v1",
                version="1.0.0",
                require_verified=False,
            )


def partition_history_windows(
    start_at: datetime,
    end_at: datetime,
    *,
    max_window: timedelta = timedelta(hours=1),
) -> tuple[tuple[datetime, datetime], ...]:
    _require_aware_interval(start_at, end_at)
    if not timedelta(0) < max_window <= timedelta(hours=1):
        raise ValueError("history max window must be positive and <= 1 hour")
    result: list[tuple[datetime, datetime]] = []
    cursor = start_at
    while cursor < end_at:
        next_cursor = min(cursor + max_window, end_at)
        result.append((cursor, next_cursor))
        cursor = next_cursor
    return tuple(result)


def partition_device_batches(
    devices: Sequence[PerceptorPullDevice],
    *,
    max_devices: int = 20,
) -> tuple[tuple[PerceptorPullDevice, ...], ...]:
    if not 1 <= max_devices <= 20:
        raise ValueError("history max device batch must be between 1 and 20")
    iterator = iter(devices)
    batches: list[tuple[PerceptorPullDevice, ...]] = []
    while batch := tuple(islice(iterator, max_devices)):
        batches.append(batch)
    return tuple(batches)


def _require_aware_interval(start_at: datetime, end_at: datetime) -> None:
    if (
        start_at.tzinfo is None
        or start_at.utcoffset() is None
        or end_at.tzinfo is None
        or end_at.utcoffset() is None
    ):
        raise PerceptorPullConfigurationError(
            "history bounds must be timezone-aware"
        )
    if end_at <= start_at:
        raise PerceptorPullConfigurationError(
            "history end must be after start"
        )


def _require_success(response: Mapping[str, Any], endpoint: str) -> None:
    if not isinstance(response, Mapping):
        raise PerceptorPullError(f"{endpoint} response must be an object")
    if str(response.get("code")) != "200" or response.get("success") is not True:
        raise PerceptorPullError(f"{endpoint} failed")


def _validate_history_series_lengths(response: Mapping[str, Any]) -> None:
    data = response.get("data")
    if data in (None, {}, []):
        return
    if isinstance(data, list):
        records = data
    elif isinstance(data, Mapping):
        nested = next(
            (
                data[key]
                for key in ("records", "list", "items", "history")
                if key in data
            ),
            None,
        )
        records = nested if isinstance(nested, list) else [data]
    else:
        raise PerceptorPullError("getHistoryData data must contain records")
    for record in records:
        if not isinstance(record, Mapping):
            raise PerceptorPullError("getHistoryData record must be an object")
        lengths: list[int] = []
        for aliases in (
            ("heart_rate", "heartRate", "heart_rates"),
            (
                "respiratory_rate",
                "respiratoryRate",
                "breath_rate",
                "breathRate",
            ),
            ("movement", "body_movement", "bodyMovement"),
        ):
            value = next(
                (record[key] for key in aliases if key in record),
                None,
            )
            if value is None or value == "":
                lengths.append(0)
            elif isinstance(value, str):
                lengths.append(len(value.split(",")))
            elif isinstance(value, list):
                lengths.append(len(value))
            else:
                raise PerceptorPullError(
                    "getHistoryData series must be list or comma-separated text"
                )
        if len(set(lengths)) != 1:
            raise PerceptorPullError(
                "getHistoryData series lengths must be equal"
            )


__all__ = [
    "HistoryReconciliationResult",
    "PerceptorPullCompatibilityProfile",
    "PerceptorPullConfigurationError",
    "PerceptorPullDevice",
    "PerceptorPullError",
    "PerceptorPullService",
    "PulledSourceReport",
    "partition_device_batches",
    "partition_history_windows",
]
