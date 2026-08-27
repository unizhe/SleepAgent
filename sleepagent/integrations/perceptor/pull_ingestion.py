"""Durable YunYun Pull intake and reconciliation on the existing Worker.

The bounded caller performs a read-only Platform request, then this module
commits the exact encrypted response and one deterministic normalization item.
The existing ingestion queue owns normalization, semantic reconciliation, and
checkpoint movement.  No Product, Episode, Agent, or model path is imported.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping, cast
from zoneinfo import ZoneInfo

from sleepagent.config import BackendKeyProvider, SleepBackendSettings
from sleepagent.domain.contracts import DataMode, DeviceBinding, bind_adapter_candidate
from sleepagent.domain.postgres_slice import (
    NormalizationLease,
    RawPayloadCipher,
    SleepSliceInvariantError,
    SleepSliceLeaseLost,
)
from sleepagent.integrations.perceptor.client import (
    GET_CURRENT_ENDPOINT,
    HISTORY_ENDPOINT,
    REALTIME_READ_ENDPOINT,
    SLEEP_REPORT_ENDPOINT,
    PerceptorPlatformClient,
    PlatformEvidencedRead,
)
from sleepagent.integrations.perceptor.pull import (
    NORMALIZER_VERSION,
    PullContractError,
    assert_requested_device_matches_binding,
    normalize_current,
    normalize_history,
    normalize_realtime,
    normalize_sleep_report,
    sleep_report_is_no_data,
    with_durable_raw_reference,
)
from sleepagent.integrations.perceptor.reconciliation import (
    PerceptorObservationReconciler,
    PerceptorReconciliationResult,
    namespace_generation_scoped_candidate,
    semantic_surface_for_pull,
)
from sleepagent.observability import (
    log_event,
    record_backend_signal,
    record_pull,
)
from sleepagent.persistence.uow import (
    ExternalIngressScope,
    UnitOfWorkFactory,
    UowScope,
)


UTC = timezone.utc
PULL_RESPONSE_PROFILE = "perceptor-platform-read-response.v1"
PULL_NORMALIZER = "perceptor_pull"
PULL_HISTORY_OVERLAP = timedelta(seconds=3)
PULL_ENDPOINTS = frozenset(
    {
        GET_CURRENT_ENDPOINT,
        REALTIME_READ_ENDPOINT,
        HISTORY_ENDPOINT,
        SLEEP_REPORT_ENDPOINT,
    }
)


class PerceptorPullIngressError(RuntimeError):
    """A response could not safely enter the durable Pull boundary."""


@dataclass(frozen=True, slots=True)
class PullRequestCoordinates:
    endpoint: str
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    report_date: date | None = None

    def __post_init__(self) -> None:
        if self.endpoint not in PULL_ENDPOINTS:
            raise ValueError("endpoint is outside the durable Pull allowlist")
        if self.endpoint == HISTORY_ENDPOINT:
            if self.window_start_at is None or self.window_end_at is None:
                raise ValueError("history requires an exact request window")
            _require_aware(self.window_start_at, "window_start_at")
            _require_aware(self.window_end_at, "window_end_at")
            if self.window_start_at.microsecond or self.window_end_at.microsecond:
                raise ValueError("history window must use the vendor's whole-second precision")
            start_utc = self.window_start_at.astimezone(UTC)
            end_utc = self.window_end_at.astimezone(UTC)
            if (
                end_utc <= start_utc
                or end_utc - start_utc > timedelta(hours=1)
            ):
                raise ValueError("history window must be positive and at most one hour")
            if self.report_date is not None:
                raise ValueError("history cannot carry a report date")
        elif self.endpoint == SLEEP_REPORT_ENDPOINT:
            if self.report_date is None:
                raise ValueError("sleep report requires a local report date")
            if self.window_start_at is not None or self.window_end_at is not None:
                raise ValueError("sleep report cannot carry a history window")
        elif any(
            value is not None
            for value in (self.window_start_at, self.window_end_at, self.report_date)
        ):
            raise ValueError("snapshot Pull cannot carry a window or report date")


@dataclass(frozen=True, slots=True)
class PerceptorPullIngressResult:
    disposition: str
    raw_ingress_record_id: str | None
    normalization_work_id: str | None
    subject_id: str | None
    device_binding_id: str | None
    duplicate: bool
    batch_identity: str
    response_semantic_sha256: str | None


@dataclass(frozen=True, slots=True)
class PullHistoryPlan:
    """One database-authorized history window with optional resume evidence."""

    window_start_at: datetime
    window_end_at: datetime
    checkpoint_cursor_at: datetime | None
    lateness_watermark_at: datetime | None
    resumed_from_checkpoint: bool

    def __post_init__(self) -> None:
        PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=self.window_start_at,
            window_end_at=self.window_end_at,
        )
        if self.resumed_from_checkpoint:
            if self.checkpoint_cursor_at is None or self.lateness_watermark_at is None:
                raise ValueError("resumed history plan requires checkpoint coordinates")
            _require_aware(self.checkpoint_cursor_at, "checkpoint_cursor_at")
            _require_aware(self.lateness_watermark_at, "lateness_watermark_at")
            if (
                self.lateness_watermark_at
                != self.checkpoint_cursor_at - PULL_HISTORY_OVERLAP
                or self.window_start_at != self.lateness_watermark_at
            ):
                raise ValueError("history resume must use the exact three-second watermark")
            if self.window_end_at < self.checkpoint_cursor_at:
                raise ValueError("history plan cannot end before its checkpoint cursor")
        elif any(
            value is not None
            for value in (self.checkpoint_cursor_at, self.lateness_watermark_at)
        ):
            raise ValueError("initial history plan cannot carry checkpoint coordinates")

    @property
    def already_processed(self) -> bool:
        """Whether the requested schedule is already covered by the checkpoint."""

        return bool(
            self.resumed_from_checkpoint
            and self.checkpoint_cursor_at is not None
            and self.window_end_at == self.checkpoint_cursor_at
        )


@dataclass(frozen=True, slots=True)
class PerceptorPullNormalizationResult:
    work_id: str
    raw_ingress_record_id: str
    canonical_observation_ids: tuple[str, ...]
    canonical_created_count: int
    push_pull_overlap_count: int
    conflict_created_count: int
    duplicate_count: int
    checkpoint_advanced: bool
    no_data: bool
    quarantined: bool = False


class DurablePerceptorPullIngress:
    """Commit one successful/failed read response to provider-neutral ingress."""

    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        client_id_sha256: str,
        cipher: RawPayloadCipher | None = None,
    ) -> None:
        if settings.perceptor_namespace_id is None:
            raise ValueError("Perceptor namespace is not configured")
        if settings.perceptor_provider_account_id is None:
            raise ValueError("Perceptor provider account is not configured")
        if not _is_sha256(client_id_sha256):
            raise ValueError("client_id_sha256 must be lowercase SHA-256")
        if cipher is None:
            key = BackendKeyProvider(settings.deployment_mode).encryption_key(
                settings.encryption_key_ref
            )
            cipher = RawPayloadCipher(key, key_id=settings.encryption_key_ref)
        self.settings = settings
        self.uow_factory = uow_factory
        self.client_id_sha256 = client_id_sha256
        self.cipher = cipher

    def plan_history_window(
        self,
        *,
        binding: DeviceBinding,
        requested_start_at: datetime,
        requested_end_at: datetime,
    ) -> PullHistoryPlan:
        """Resolve the next bounded window from the durable Worker checkpoint.

        The API role cannot read the checkpoint table.  A narrowly granted
        SECURITY DEFINER function validates the exact namespace generation,
        provider account, subject, and binding before returning timestamps
        only.  Once a checkpoint exists, caller-supplied starts are never
        trusted: the database returns ``cursor_at - 3 seconds``.
        """

        PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=requested_start_at,
            window_end_at=requested_end_at,
        )
        if binding.data_mode != DataMode.LIVE:
            raise ValueError("durable Perceptor Pull requires a live binding")
        if binding.provider_id != "perceptor":
            raise ValueError("durable Perceptor Pull requires a Perceptor binding")
        if binding.provider_account_id != self.settings.perceptor_provider_account_id:
            raise ValueError("binding and configured provider account disagree")
        namespace = cast(str, self.settings.perceptor_namespace_id)
        account = cast(str, self.settings.perceptor_provider_account_id)
        scope = ExternalIngressScope(
            namespace_id=namespace,
            namespace_generation=self.settings.perceptor_namespace_generation,
            service_principal_id=self.settings.service_principal_id,
            authorization_epoch=self.settings.perceptor_authorization_epoch,
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_plan_perceptor_history("
                    + ",".join(["%s"] * 8)
                    + ")",
                    (
                        namespace,
                        self.settings.perceptor_namespace_generation,
                        account,
                        binding.device_binding_id,
                        binding.binding_version,
                        binding.subject_id,
                        requested_start_at,
                        requested_end_at,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None or len(row) != 5:
            raise PerceptorPullIngressError(
                "Perceptor history planner returned a malformed result"
            )
        if not isinstance(row[4], bool):
            raise PerceptorPullIngressError(
                "Perceptor history planner returned an invalid resume flag"
            )
        plan = PullHistoryPlan(
            window_start_at=_aware_datetime(row[0], "window_start_at"),
            window_end_at=_aware_datetime(row[1], "window_end_at"),
            checkpoint_cursor_at=(
                None
                if row[2] is None
                else _aware_datetime(row[2], "checkpoint_cursor_at")
            ),
            lateness_watermark_at=(
                None
                if row[3] is None
                else _aware_datetime(row[3], "lateness_watermark_at")
            ),
            resumed_from_checkpoint=row[4],
        )
        requested_end_utc = requested_end_at.astimezone(UTC)
        if plan.already_processed:
            if (
                plan.checkpoint_cursor_at is None
                or requested_end_utc > plan.checkpoint_cursor_at
            ):
                raise PerceptorPullIngressError(
                    "Perceptor history planner returned an invalid no-op"
                )
        elif plan.window_end_at != requested_end_utc:
            raise PerceptorPullIngressError(
                "Perceptor history planner changed the requested end"
            )
        if not plan.resumed_from_checkpoint and (
            plan.window_start_at != requested_start_at.astimezone(UTC)
        ):
            raise PerceptorPullIngressError(
                "Perceptor history planner changed an initial start"
            )
        return plan

    def accept(
        self,
        read: PlatformEvidencedRead[Any],
        *,
        binding: DeviceBinding,
        coordinates: PullRequestCoordinates,
    ) -> PerceptorPullIngressResult:
        if read.endpoint != coordinates.endpoint:
            raise ValueError("read evidence and request coordinates disagree")
        if binding.data_mode != DataMode.LIVE:
            raise ValueError("durable Perceptor Pull requires a live binding")
        if binding.provider_id != "perceptor":
            raise ValueError("durable Perceptor Pull requires a Perceptor binding")
        if binding.provider_account_id != self.settings.perceptor_provider_account_id:
            raise ValueError("binding and configured provider account disagree")
        requested_name = binding.provider_device.provider_device_name or ""
        assert_requested_device_matches_binding(requested_name, binding.provider_device)
        evidence = read.evidence
        _require_aware(evidence.requested_at, "requested_at")
        _require_aware(evidence.received_at, "received_at")
        if evidence.received_at < evidence.requested_at:
            raise ValueError("response receipt precedes request")
        _assert_pull_coordinates_within_binding(
            binding,
            coordinates,
            requested_at=evidence.requested_at,
        )
        exact_sha256 = evidence.raw_sha256
        response_valid = evidence.http_status == 200
        response_data: object | None = None
        try:
            response_data = parse_platform_success_response(evidence.raw_body)
            if _semantic_json(response_data) != _semantic_json(read.data):
                raise PerceptorPullIngressError(
                    "parsed read differs from exact response evidence"
                )
        except (PerceptorPullIngressError, TypeError, ValueError):
            response_valid = False
        response_semantic_sha256 = (
            hashlib.sha256(_semantic_json(response_data)).hexdigest()
            if response_valid
            else exact_sha256
        )
        response_has_data = False
        response_device_id: str | None = None
        response_device_name: str | None = None
        if response_valid:
            try:
                response_has_data = _response_has_data(
                    coordinates.endpoint,
                    cast(object, response_data),
                )
                response_device_id, response_device_name = _response_device_identity(
                    coordinates.endpoint,
                    cast(object, response_data),
                    binding,
                )
            except (PullContractError, TypeError, ValueError):
                response_valid = False
                response_has_data = False

        namespace = cast(str, self.settings.perceptor_namespace_id)
        account = cast(str, self.settings.perceptor_provider_account_id)
        descriptor = _request_descriptor(
            namespace,
            self.settings.perceptor_namespace_generation,
            account,
            binding,
            coordinates,
        )
        raw_identity = _stable_digest(
            "perceptor-pull-raw.v1",
            descriptor,
            exact_sha256,
        )
        batch_identity = _stable_digest(
            "perceptor-pull-batch.v1",
            descriptor,
            response_semantic_sha256,
            NORMALIZER_VERSION,
        )
        raw_id = f"perceptor:pull:raw:{raw_identity}"
        work_id = f"perceptor:pull:work:{batch_identity}"
        stream_key = _stream_key(
            binding,
            coordinates.endpoint,
            namespace_generation=self.settings.perceptor_namespace_generation,
        )
        checkpoint_cursor_at, lateness_watermark_at = _checkpoint_coordinates(
            binding,
            coordinates,
            evidence.received_at,
        )
        encrypted = self.cipher.encrypt(
            evidence.raw_body,
            aad=perceptor_pull_raw_aad(
                namespace,
                self.settings.perceptor_namespace_generation,
                raw_id,
            ),
        )
        safe_metadata = {
            "schema_version": "perceptor_pull_raw_metadata.v1",
            "source_channel": "PULL",
            "endpoint": coordinates.endpoint,
            "raw_sha256": exact_sha256,
            "response_semantic_sha256": response_semantic_sha256,
            "normalizer_version": NORMALIZER_VERSION,
            "batch_identity": batch_identity,
            "stream_key": stream_key,
            "checkpoint_cursor_at": checkpoint_cursor_at.isoformat(),
            "lateness_watermark_at": lateness_watermark_at.isoformat(),
            "requested_window_start": _iso_or_none(coordinates.window_start_at),
            "requested_window_end": _iso_or_none(coordinates.window_end_at),
            "requested_report_date": (
                None if coordinates.report_date is None else coordinates.report_date.isoformat()
            ),
            "response_valid": response_valid,
            "response_has_data": response_has_data,
            "provider_device_key": _provider_device_key(
                binding,
                namespace_generation=self.settings.perceptor_namespace_generation,
            ),
        }
        ids = {
            name: f"perceptor:pull:{name}:{_stable_digest(raw_identity, name)}"
            for name in ("intake", "quarantine-receipt", "quarantine")
        }
        scope = ExternalIngressScope(
            namespace_id=namespace,
            namespace_generation=self.settings.perceptor_namespace_generation,
            service_principal_id=self.settings.service_principal_id,
            authorization_epoch=self.settings.perceptor_authorization_epoch,
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_ingest_perceptor_pull("
                    + ",".join(["%s"] * 30)
                    + ")",
                    (
                        namespace,
                        self.settings.perceptor_namespace_generation,
                        account,
                        self.client_id_sha256,
                        binding.provider_device.provider_device_id,
                        binding.provider_device.provider_device_name,
                        response_device_id,
                        response_device_name,
                        coordinates.endpoint,
                        coordinates.window_start_at,
                        coordinates.window_end_at,
                        coordinates.report_date,
                        evidence.requested_at,
                        evidence.received_at,
                        raw_identity,
                        batch_identity,
                        exact_sha256,
                        encrypted,
                        self.cipher.key_id,
                        "application/json",
                        len(evidence.raw_body),
                        evidence.received_at
                        + timedelta(seconds=self.settings.raw_retention_seconds),
                        response_valid,
                        response_has_data,
                        raw_id,
                        work_id,
                        ids["intake"],
                        ids["quarantine-receipt"],
                        ids["quarantine"],
                        json.dumps(safe_metadata, sort_keys=True, separators=(",", ":")),
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise PerceptorPullIngressError(
                    "Perceptor Pull ingress function returned no result"
                )
            if len(row) != 6:
                raise PerceptorPullIngressError(
                    "Perceptor Pull ingress function returned a malformed result"
                )
            if row[3] is not None and str(row[3]) != binding.subject_id:
                raise PerceptorPullIngressError(
                    "durable Pull subject does not match the asserted DeviceBinding"
                )
            if row[4] is not None and str(row[4]) != binding.device_binding_id:
                raise PerceptorPullIngressError(
                    "durable Pull resolution does not match the asserted DeviceBinding"
                )
            uow.commit()
        result = PerceptorPullIngressResult(
            disposition=str(row[0]),
            raw_ingress_record_id=None if row[1] is None else str(row[1]),
            normalization_work_id=None if row[2] is None else str(row[2]),
            subject_id=None if row[3] is None else str(row[3]),
            device_binding_id=None if row[4] is None else str(row[4]),
            duplicate=bool(row[5]),
            batch_identity=batch_identity,
            response_semantic_sha256=response_semantic_sha256,
        )
        record_backend_signal(category="pull", outcome="raw_committed")
        log_event(
            "pull_raw_committed",
            endpoint=coordinates.endpoint,
            disposition=result.disposition,
            duplicate=result.duplicate,
            raw_ingress_record_id=result.raw_ingress_record_id,
            normalization_work_id=result.normalization_work_id,
            batch_identity=batch_identity,
        )
        if result.duplicate:
            record_backend_signal(category="pull", outcome="duplicate")
            log_event("pull_duplicate", endpoint=coordinates.endpoint, batch_identity=batch_identity)
        return result


class PerceptorPullBackfillRunner:
    """Bounded read-only Platform calls followed by durable response intake."""

    def __init__(
        self,
        client: PerceptorPlatformClient,
        ingress: DurablePerceptorPullIngress,
        binding: DeviceBinding,
    ) -> None:
        self.client = client
        self.ingress = ingress
        self.binding = binding
        self.device_name = binding.provider_device.provider_device_name or ""
        self.home_id = binding.provider_device.home_id or ""
        assert_requested_device_matches_binding(self.device_name, binding.provider_device)

    def pull_history(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> PerceptorPullIngressResult:
        plan = self.ingress.plan_history_window(
            binding=self.binding,
            requested_start_at=start_at,
            requested_end_at=end_at,
        )
        if plan.already_processed:
            return self._history_checkpoint_noop(start_at=start_at, end_at=end_at)
        coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=plan.window_start_at,
            window_end_at=plan.window_end_at,
        )
        return self._run(
            coordinates,
            lambda: self.client.get_history_evidenced(
                device_names=(self.device_name,),
                start_at=plan.window_start_at,
                end_at=plan.window_end_at,
            ),
        )

    def _history_checkpoint_noop(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> PerceptorPullIngressResult:
        coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=start_at,
            window_end_at=end_at,
        )
        namespace = cast(str, self.ingress.settings.perceptor_namespace_id)
        batch_identity = _stable_digest(
            "perceptor-pull-checkpoint-noop.v1",
            _request_descriptor(
                namespace,
                self.ingress.settings.perceptor_namespace_generation,
                cast(str, self.ingress.settings.perceptor_provider_account_id),
                self.binding,
                coordinates,
            ),
        )
        result = PerceptorPullIngressResult(
            disposition="checkpoint_already_advanced",
            raw_ingress_record_id=None,
            normalization_work_id=None,
            subject_id=self.binding.subject_id,
            device_binding_id=self.binding.device_binding_id,
            duplicate=True,
            batch_identity=batch_identity,
            response_semantic_sha256=None,
        )
        record_backend_signal(category="pull", outcome="duplicate")
        record_backend_signal(category="pull", outcome="checkpoint_held")
        log_event(
            "pull_duplicate",
            endpoint=HISTORY_ENDPOINT,
            disposition=result.disposition,
            batch_identity=batch_identity,
        )
        log_event(
            "pull_checkpoint_held",
            endpoint=HISTORY_ENDPOINT,
            reason="already_advanced",
        )
        return result

    def pull_current(self) -> PerceptorPullIngressResult:
        coordinates = PullRequestCoordinates(endpoint=GET_CURRENT_ENDPOINT)
        return self._run(
            coordinates,
            lambda: self.client.get_current_evidenced(
                device_name=self.device_name, home_id=self.home_id
            ),
        )

    def pull_realtime_once(self) -> PerceptorPullIngressResult:
        coordinates = PullRequestCoordinates(endpoint=REALTIME_READ_ENDPOINT)
        return self._run(
            coordinates,
            lambda: self.client.get_realtime_evidenced(
                device_name=self.device_name, home_id=self.home_id
            ),
        )

    def pull_sleep_report(self, report_date: date) -> PerceptorPullIngressResult:
        coordinates = PullRequestCoordinates(
            endpoint=SLEEP_REPORT_ENDPOINT, report_date=report_date
        )
        return self._run(
            coordinates,
            lambda: self.client.get_sleep_report_evidenced(
                device_name=self.device_name,
                home_id=self.home_id,
                report_date=report_date,
            ),
        )

    def _run(
        self,
        coordinates: PullRequestCoordinates,
        call: Callable[[], PlatformEvidencedRead[Any]],
    ) -> PerceptorPullIngressResult:
        record_pull(source="perceptor", endpoint=coordinates.endpoint)
        record_backend_signal(category="pull", outcome="started")
        log_event("pull_started", endpoint=coordinates.endpoint)
        try:
            result = self.ingress.accept(
                call(), binding=self.binding, coordinates=coordinates
            )
        except Exception as exc:
            record_backend_signal(category="pull", outcome="failed")
            log_event("pull_failed", endpoint=coordinates.endpoint, error_type=type(exc).__name__)
            raise
        record_backend_signal(category="pull", outcome="succeeded")
        log_event("pull_succeeded", endpoint=coordinates.endpoint, disposition=result.disposition)
        return result


class PerceptorPullNormalizationProcessor:
    """Two-phase Pull reconciliation with a crash-safe durable checkpoint."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        cipher: RawPayloadCipher,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.cipher = cipher
        self.now_factory = now_factory
        self.fault_injector = fault_injector
        self.reconciler = PerceptorObservationReconciler()

    def process(
        self,
        scope: UowScope,
        lease: NormalizationLease,
    ) -> PerceptorPullNormalizationResult:
        if scope.data_mode != "live" or scope.subject_id is None:
            raise SleepSliceInvariantError(
                "Perceptor Pull normalization requires exact live subject scope"
            )
        committed_at = self.now_factory()
        _require_aware(committed_at, "committed_at")
        loaded = self._load(scope, lease)
        try:
            normalized = self._normalize_loaded(loaded)
        except (
            PerceptorPullIngressError,
            PullContractError,
            ValueError,
            SleepSliceInvariantError,
        ) as exc:
            self._quarantine(
                scope,
                lease,
                raw_ingress_record_id=loaded["raw_ingress_record_id"],
                detail_code=type(exc).__name__,
                committed_at=committed_at,
            )
            record_backend_signal(category="pull", outcome="normalization_failed")
            log_event(
                "normalization_failed",
                endpoint=loaded["work_json"].get("endpoint"),
                raw_ingress_record_id=loaded["raw_ingress_record_id"],
                error_type=type(exc).__name__,
            )
            return PerceptorPullNormalizationResult(
                work_id=lease.work_id,
                raw_ingress_record_id=loaded["raw_ingress_record_id"],
                canonical_observation_ids=(),
                canonical_created_count=0,
                push_pull_overlap_count=0,
                conflict_created_count=0,
                duplicate_count=0,
                checkpoint_advanced=False,
                no_data=False,
                quarantined=True,
            )

        summary = self._commit_reconciliation(
            scope,
            lease,
            loaded=loaded,
            normalized=normalized,
            committed_at=committed_at,
        )
        if self.fault_injector is not None:
            self.fault_injector("after_reconciliation_commit")
        checkpoint_advanced = self._advance_checkpoint_and_finalize(
            scope,
            lease,
            loaded=loaded,
            reconciliation_summary=summary,
            committed_at=committed_at,
        )
        _emit_reconciliation_observability(loaded["work_json"], summary, checkpoint_advanced)
        return PerceptorPullNormalizationResult(
            work_id=lease.work_id,
            raw_ingress_record_id=loaded["raw_ingress_record_id"],
            canonical_observation_ids=tuple(summary["canonical_observation_ids"]),
            canonical_created_count=int(summary["canonical_created_count"]),
            push_pull_overlap_count=int(summary["push_pull_overlap_count"]),
            conflict_created_count=int(summary["conflict_created_count"]),
            duplicate_count=int(summary["duplicate_count"]),
            checkpoint_advanced=checkpoint_advanced,
            no_data=bool(summary["no_data"]),
        )

    def _load(self, scope: UowScope, lease: NormalizationLease) -> dict[str, Any]:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT raw.raw_ingress_record_id, raw.encrypted_payload,
                           raw.encryption_key_id,
                           raw.pre_normalization_payload_sha256,
                           raw.provider_account_id, raw.request_signed_at,
                           raw.received_at, work.work_json, binding.binding_json
                    FROM public.sleep_domain_normalization_work AS work
                    JOIN public.sleep_domain_raw_inbox AS raw
                      ON raw.raw_ingress_record_id = work.raw_ingress_record_id
                     AND raw.namespace_id = work.namespace_id
                     AND raw.data_mode = work.data_mode
                     AND raw.subject_id = work.subject_id
                    JOIN public.sleep_domain_device_bindings AS binding
                      ON binding.device_binding_id = work.work_json ->> 'device_binding_id'
                     AND binding.namespace_id = work.namespace_id
                     AND binding.data_mode = work.data_mode
                     AND binding.subject_id = work.subject_id
                    WHERE work.work_id = %s AND work.namespace_id = %s
                      AND work.data_mode = 'live'
                      AND work.namespace_generation = %s AND work.subject_id = %s
                      AND work.work_json ->> 'normalizer' = %s
                      AND raw.signature_verification = 'not_provided'
                      AND raw.signature_profile = %s
                      AND raw.encryption_protocol_version = 1
                      AND work.status = 'running' AND work.lease_generation = %s
                      AND work.fencing_token = %s AND work.worker_instance = %s
                      AND work.lease_expires_at > clock_timestamp()
                    """,
                    (
                        lease.work_id,
                        scope.namespace_id,
                        scope.namespace_generation,
                        scope.subject_id,
                        PULL_NORMALIZER,
                        PULL_RESPONSE_PROFILE,
                        lease.lease_generation,
                        lease.fencing_token,
                        lease.worker_instance,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise SleepSliceLeaseLost("Perceptor Pull normalization fence was rejected")
        if str(row[2]) != self.cipher.key_id:
            raise SleepSliceInvariantError("raw payload encryption key is unavailable")
        return {
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "raw_ingress_record_id": str(row[0]),
            "encrypted_payload": bytes(row[1]),
            "payload_sha256": str(row[3]),
            "provider_account_id": str(row[4]),
            "requested_at": row[5],
            "received_at": row[6],
            "work_json": _json_mapping(row[7]),
            "binding_json": _json_mapping(row[8]),
        }

    def _normalize_loaded(self, loaded: Mapping[str, Any]) -> dict[str, Any]:
        raw_id = str(loaded["raw_ingress_record_id"])
        work = cast(dict[str, Any], loaded["work_json"])
        raw = self.cipher.decrypt(
            cast(bytes, loaded["encrypted_payload"]),
            aad=perceptor_pull_raw_aad(
                str(loaded["namespace_id"]),
                int(loaded["namespace_generation"]),
                raw_id,
            ),
        )
        if not raw:  # pragma: no cover - RawPayloadCipher already authenticates
            raise SleepSliceInvariantError("decrypted Pull response is empty")
        if hashlib.sha256(raw).hexdigest() != loaded["payload_sha256"]:
            raise SleepSliceInvariantError("raw Pull response hash mismatch")
        data = parse_platform_success_response(raw)
        if hashlib.sha256(_semantic_json(data)).hexdigest() != work.get(
            "response_semantic_sha256"
        ):
            raise SleepSliceInvariantError("Pull response semantic hash mismatch")
        binding = DeviceBinding.model_validate(loaded["binding_json"])
        common = {
            "provider_account_id": str(loaded["provider_account_id"]),
            "provider_device": binding.provider_device,
            "raw_sha256": str(loaded["payload_sha256"]),
            "requested_at": loaded["requested_at"],
            "received_at": loaded["received_at"],
        }
        endpoint = str(work.get("endpoint"))
        no_data = False
        if endpoint == HISTORY_ENDPOINT:
            if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
                raise PullContractError("history response data must be a list of objects")
            if not data:
                no_data = True
                result = None
            else:
                requested_window_start = _aware_from_text(
                    work.get("requested_window_start"),
                    "requested_window_start",
                )
                requested_window_end = _aware_from_text(
                    work.get("requested_window_end"),
                    "requested_window_end",
                )
                result = normalize_history(
                    data,
                    binding_timezone_name=binding.timezone_name,
                    requested_window_start=requested_window_start,
                    requested_window_end=requested_window_end,
                    **common,
                )
        elif endpoint == GET_CURRENT_ENDPOINT:
            if not isinstance(data, Mapping):
                raise PullContractError("current response data must be an object")
            no_data = not data
            result = None if no_data else normalize_current(data, **common)
        elif endpoint == REALTIME_READ_ENDPOINT:
            if not isinstance(data, Mapping):
                raise PullContractError("realtime response data must be an object")
            no_data = not data
            result = (
                None
                if no_data
                else normalize_realtime(
                    data,
                    binding_timezone_name=binding.timezone_name,
                    **common,
                )
            )
        elif endpoint == SLEEP_REPORT_ENDPOINT:
            if not isinstance(data, Mapping):
                raise PullContractError("sleep report response data must be an object")
            report_date = date.fromisoformat(
                _required_text(
                    work.get("requested_report_date"),
                    "requested_report_date",
                )
            )
            no_data = sleep_report_is_no_data(data)
            result = (
                None
                if no_data
                else normalize_sleep_report(
                    data,
                    report_date=report_date,
                    binding_timezone_name=binding.timezone_name,
                    **common,
                )
            )
        else:
            raise PullContractError("unsupported durable Pull endpoint")
        response_has_data = work.get("response_has_data")
        if not isinstance(response_has_data, bool):
            raise SleepSliceInvariantError(
                "persisted Pull response_has_data must be a strict boolean"
            )
        if response_has_data != (not no_data):
            raise SleepSliceInvariantError(
                "persisted Pull response_has_data contradicts normalization"
            )
        if result is not None and not result.candidates:
            raise PullContractError(
                "non-empty Pull response produced no canonical candidates"
            )
        candidates = (
            ()
            if result is None
            else tuple(
                namespace_generation_scoped_candidate(
                    candidate,
                    namespace_id=str(loaded["namespace_id"]),
                    namespace_generation=int(loaded["namespace_generation"]),
                )
                for candidate in with_durable_raw_reference(
                    result, raw_id
                ).candidates
            )
        )
        observations = tuple(bind_adapter_candidate(candidate, binding) for candidate in candidates)
        history_classification = (
            None if result is None else result.history_window_classification
        )
        return {
            "endpoint": endpoint,
            "binding": binding,
            "candidates": candidates,
            "observations": observations,
            "no_data": no_data,
            "unknown_fields": () if result is None else result.unknown_fields,
            "history_window_classification": (
                None
                if history_classification is None
                else {
                    "requested_start_at": (
                        history_classification.requested_start_at.isoformat()
                    ),
                    "requested_end_at": (
                        history_classification.requested_end_at.isoformat()
                    ),
                    "earliest_reconstructed_at": (
                        history_classification.earliest_reconstructed_at.isoformat()
                    ),
                    "latest_reconstructed_at": (
                        history_classification.latest_reconstructed_at.isoformat()
                    ),
                    "before_window_candidate_count": (
                        history_classification.before_window_candidate_count
                    ),
                    "in_window_candidate_count": (
                        history_classification.in_window_candidate_count
                    ),
                    "after_window_candidate_count": (
                        history_classification.after_window_candidate_count
                    ),
                }
            ),
        }

    def _commit_reconciliation(
        self,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        loaded: Mapping[str, Any],
        normalized: Mapping[str, Any],
        committed_at: datetime,
    ) -> dict[str, Any]:
        receipt_id = _phase_receipt_id(lease.work_id)
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                self._lock_fence(cursor, scope, lease)
                cursor.execute(
                    """
                    SELECT receipt_json
                    FROM public.sleep_domain_processing_receipts
                    WHERE receipt_id = %s AND namespace_id = %s AND data_mode = 'live'
                    """,
                    (receipt_id, scope.namespace_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    summary = _json_mapping(existing[0])["reconciliation_summary"]
                    if not isinstance(summary, Mapping):
                        raise SleepSliceInvariantError("Pull reconciliation receipt is malformed")
                    uow.commit()
                    return dict(summary)
                reconciliations: list[PerceptorReconciliationResult] = []
                for candidate, observation in zip(
                    normalized["candidates"], normalized["observations"], strict=True
                ):
                    reconciliations.append(
                        self.reconciler.reconcile(
                            cursor,
                            scope,
                            raw_ingress_record_id=str(loaded["raw_ingress_record_id"]),
                            candidate=candidate,
                            observation=observation,
                            semantic_surface=semantic_surface_for_pull(
                                str(normalized["endpoint"]), observation
                            ),
                            committed_at=committed_at,
                        )
                    )
                if normalized["endpoint"] == SLEEP_REPORT_ENDPOINT:
                    self._insert_source_report(
                        cursor,
                        scope,
                        loaded=loaded,
                        normalized=normalized,
                        committed_at=committed_at,
                    )
                summary = {
                    "canonical_observation_ids": [
                        item.canonical_observation_id for item in reconciliations
                    ],
                    "canonical_created_count": sum(
                        item.canonical_created for item in reconciliations
                    ),
                    "push_pull_overlap_count": sum(
                        item.push_pull_overlap for item in reconciliations
                    ),
                    "conflict_created_count": sum(
                        item.conflict_created_count for item in reconciliations
                    ),
                    "duplicate_count": sum(item.duplicate for item in reconciliations),
                    "last_canonical_observation_id": (
                        None
                        if not reconciliations
                        else sorted(
                            item.canonical_observation_id for item in reconciliations
                        )[-1]
                    ),
                    "no_data": bool(normalized["no_data"]),
                }
                if normalized.get("history_window_classification") is not None:
                    summary["history_window_classification"] = normalized[
                        "history_window_classification"
                    ]
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_processing_receipts (
                      receipt_id, namespace_id, data_mode, raw_ingress_record_id,
                      stage, outcome, receipt_json, occurred_at
                    ) VALUES (%s, %s, 'live', %s, 'normalization', 'succeeded', %s::jsonb, %s)
                    """,
                    (
                        receipt_id,
                        scope.namespace_id,
                        loaded["raw_ingress_record_id"],
                        json.dumps(
                            {
                                "schema_version": "perceptor_pull_reconciliation_receipt.v1",
                                "raw_ingress_record_id": loaded["raw_ingress_record_id"],
                                "reconciliation_summary": summary,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        committed_at,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE public.sleep_domain_normalization_work
                    SET work_json = work_json || %s::jsonb, updated_at = %s
                    WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
                      AND namespace_generation = %s AND subject_id = %s
                      AND status = 'running' AND lease_generation = %s
                      AND fencing_token = %s AND worker_instance = %s
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (
                        json.dumps(
                            {"reconciliation_committed": True},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        committed_at,
                        lease.work_id,
                        scope.namespace_id,
                        scope.namespace_generation,
                        scope.subject_id,
                        lease.lease_generation,
                        lease.fencing_token,
                        lease.worker_instance,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SleepSliceLeaseLost("Pull fence expired before reconciliation commit")
            finally:
                cursor.close()
            uow.commit()
        return summary

    def _advance_checkpoint_and_finalize(
        self,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        loaded: Mapping[str, Any],
        reconciliation_summary: Mapping[str, Any],
        committed_at: datetime,
    ) -> bool:
        work = cast(Mapping[str, Any], loaded["work_json"])
        cursor_at = _aware_from_text(work.get("checkpoint_cursor_at"), "checkpoint_cursor_at")
        watermark = _aware_from_text(work.get("lateness_watermark_at"), "lateness_watermark_at")
        stream_key = _required_text(work.get("stream_key"), "stream_key")
        data_surface = _required_text(work.get("data_surface"), "data_surface")
        binding_id = _required_text(work.get("device_binding_id"), "device_binding_id")
        binding_version = int(work.get("binding_version", 0))
        if binding_version < 1:
            raise SleepSliceInvariantError("binding_version is invalid")
        overlap_seconds = int(work.get("overlap_seconds", 0))
        canonical_ids = tuple(
            sorted(
                _required_text(value, "canonical_observation_ids[]")
                for value in reconciliation_summary.get(
                    "canonical_observation_ids", ()
                )
            )
        )
        no_data_value = reconciliation_summary.get("no_data")
        if not isinstance(no_data_value, bool):
            raise SleepSliceInvariantError(
                "checkpoint reconciliation no_data must be a strict boolean"
            )
        no_data = no_data_value
        if canonical_ids and no_data:
            raise SleepSliceInvariantError(
                "checkpoint evidence cannot be both canonical and no-data"
            )
        if not canonical_ids and not no_data:
            raise SleepSliceInvariantError(
                "checkpoint requires canonical evidence or an exact no-data receipt"
            )
        last_canonical_observation_id = (
            canonical_ids[-1] if canonical_ids else None
        )
        checkpoint_id = "perceptor:pull:checkpoint:" + _stable_digest(
            scope.namespace_id, scope.data_mode, str(work.get("provider_account_id")), stream_key
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                self._lock_fence(cursor, scope, lease)
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_pull_checkpoints (
                      checkpoint_id, namespace_id, data_mode, provider_id,
                      provider_account_id, stream_key, cursor_at,
                      lateness_watermark_at, cas_version, updated_at,
                      protocol_version, namespace_generation, run_id, arm_id,
                      subject_id, device_binding_id, binding_version, data_surface,
                      overlap_seconds, last_raw_ingress_record_id,
                      last_normalization_work_id,
                      last_canonical_observation_id, last_canonical_commit_at
                    ) VALUES (
                      %s, %s, 'live', 'perceptor', %s, %s, %s, %s, 0, %s,
                      2, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (
                      namespace_id, data_mode, provider_id,
                      provider_account_id, stream_key
                    ) DO UPDATE SET
                      cursor_at = EXCLUDED.cursor_at,
                      lateness_watermark_at = EXCLUDED.lateness_watermark_at,
                      cas_version = sleep_domain_pull_checkpoints.cas_version + 1,
                      updated_at = EXCLUDED.updated_at,
                      last_raw_ingress_record_id = EXCLUDED.last_raw_ingress_record_id,
                      last_normalization_work_id = EXCLUDED.last_normalization_work_id,
                      last_canonical_observation_id =
                        EXCLUDED.last_canonical_observation_id,
                      last_canonical_commit_at = EXCLUDED.last_canonical_commit_at
                    WHERE sleep_domain_pull_checkpoints.cursor_at < EXCLUDED.cursor_at
                    RETURNING checkpoint_id
                    """,
                    (
                        checkpoint_id,
                        scope.namespace_id,
                        work.get("provider_account_id"),
                        stream_key,
                        cursor_at,
                        watermark,
                        committed_at,
                        scope.namespace_generation,
                        scope.subject_id,
                        binding_id,
                        binding_version,
                        data_surface,
                        overlap_seconds,
                        loaded["raw_ingress_record_id"],
                        lease.work_id,
                        last_canonical_observation_id,
                        committed_at,
                    ),
                )
                advanced = cursor.fetchone() is not None
                self._finalize_work(cursor, scope, lease, committed_at=committed_at)
            finally:
                cursor.close()
            uow.commit()
        return advanced

    @staticmethod
    def _lock_fence(cursor: Any, scope: UowScope, lease: NormalizationLease) -> None:
        cursor.execute(
            """
            SELECT work_id
            FROM public.sleep_domain_normalization_work
            WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
              AND namespace_generation = %s AND subject_id = %s
              AND work_json ->> 'normalizer' = %s
              AND status = 'running' AND lease_generation = %s
              AND fencing_token = %s AND worker_instance = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (
                lease.work_id,
                scope.namespace_id,
                scope.namespace_generation,
                scope.subject_id,
                PULL_NORMALIZER,
                lease.lease_generation,
                lease.fencing_token,
                lease.worker_instance,
            ),
        )
        if cursor.fetchone() is None:
            raise SleepSliceLeaseLost("Perceptor Pull work fence was rejected")

    @staticmethod
    def _finalize_work(
        cursor: Any,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        committed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE public.sleep_domain_normalization_work
            SET status = 'succeeded', updated_at = %s, last_error_code = NULL,
                lease_owner = NULL, lease_expires_at = NULL,
                fencing_token = NULL, worker_instance = NULL, heartbeat_at = NULL
            WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
              AND namespace_generation = %s AND subject_id = %s
              AND status = 'running' AND lease_generation = %s
              AND fencing_token = %s AND worker_instance = %s
              AND lease_expires_at > clock_timestamp()
            """,
            (
                committed_at,
                lease.work_id,
                scope.namespace_id,
                scope.namespace_generation,
                scope.subject_id,
                lease.lease_generation,
                lease.fencing_token,
                lease.worker_instance,
            ),
        )
        if cursor.rowcount != 1:
            raise SleepSliceLeaseLost("Pull fence expired before checkpoint finalization")

    def _quarantine(
        self,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        raw_ingress_record_id: str,
        detail_code: str,
        committed_at: datetime,
    ) -> None:
        receipt_id = _stable_prefixed_id("receipt", lease.work_id, "quarantine")
        quarantine_id = _stable_prefixed_id("quarantine", lease.work_id)
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                self._lock_fence(cursor, scope, lease)
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_processing_receipts (
                      receipt_id, namespace_id, data_mode, raw_ingress_record_id,
                      stage, outcome, quarantine_reason, receipt_json, occurred_at
                    ) VALUES (
                      %s, %s, 'live', %s, 'normalization', 'quarantined',
                      'malformed_payload', %s::jsonb, %s
                    ) ON CONFLICT (receipt_id) DO NOTHING
                    """,
                    (
                        receipt_id,
                        scope.namespace_id,
                        raw_ingress_record_id,
                        json.dumps(
                            {
                                "schema_version": "perceptor_pull_quarantine.v1",
                                "reason": "malformed_payload",
                                "detail_code": detail_code,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        committed_at,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_quarantine (
                      quarantine_id, namespace_id, data_mode,
                      raw_ingress_record_id, reason, detail_code, receipt_id,
                      quarantine_json, quarantined_at
                    ) VALUES (
                      %s, %s, 'live', %s, 'malformed_payload', %s, %s,
                      %s::jsonb, %s
                    ) ON CONFLICT (quarantine_id) DO NOTHING
                    """,
                    (
                        quarantine_id,
                        scope.namespace_id,
                        raw_ingress_record_id,
                        detail_code,
                        receipt_id,
                        json.dumps(
                            {
                                "schema_version": "perceptor_pull_quarantine.v1",
                                "reason": "malformed_payload",
                                "detail_code": detail_code,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        committed_at,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE public.sleep_domain_normalization_work
                    SET status = 'quarantined', updated_at = %s,
                        last_error_code = 'perceptor_pull_normalization_quarantined',
                        lease_owner = NULL, lease_expires_at = NULL,
                        fencing_token = NULL, worker_instance = NULL, heartbeat_at = NULL
                    WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
                      AND namespace_generation = %s AND subject_id = %s
                      AND status = 'running' AND lease_generation = %s
                      AND fencing_token = %s AND worker_instance = %s
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (
                        committed_at,
                        lease.work_id,
                        scope.namespace_id,
                        scope.namespace_generation,
                        scope.subject_id,
                        lease.lease_generation,
                        lease.fencing_token,
                        lease.worker_instance,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SleepSliceLeaseLost("Pull fence expired before quarantine")
            finally:
                cursor.close()
            uow.commit()

    @staticmethod
    def _insert_source_report(
        cursor: Any,
        scope: UowScope,
        *,
        loaded: Mapping[str, Any],
        normalized: Mapping[str, Any],
        committed_at: datetime,
    ) -> None:
        work = cast(Mapping[str, Any], loaded["work_json"])
        report_date_text = _required_text(work.get("requested_report_date"), "requested_report_date")
        report_date = date.fromisoformat(report_date_text)
        content_sha256 = _required_text(
            work.get("response_semantic_sha256"), "response_semantic_sha256"
        )
        provider_device_key = _required_text(
            work.get("provider_device_key"), "provider_device_key"
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"{scope.namespace_id}\x1f{scope.namespace_generation}\x1f"
                f"{provider_device_key}\x1f{report_date}",
            ),
        )
        source_report_id = _stable_prefixed_id(
            "source-report",
            scope.namespace_id,
            scope.namespace_generation,
            provider_device_key,
            report_date,
            content_sha256,
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_source_reports (
              source_report_version_id, namespace_id, data_mode, provider_id,
              provider_account_id, provider_device_key, local_report_date,
              report_version, content_sha256, raw_ingress_record_id,
              is_empty, fetched_at
            ) SELECT %s, %s, 'live', 'perceptor', %s, %s, %s,
                     COALESCE(MAX(report_version), 0) + 1, %s, %s, %s, %s
              FROM public.sleep_domain_source_reports
             WHERE namespace_id = %s AND data_mode = 'live'
               AND provider_id = 'perceptor' AND provider_account_id = %s
               AND provider_device_key = %s AND local_report_date = %s
            ON CONFLICT (
              namespace_id, data_mode, provider_id, provider_account_id,
              provider_device_key, local_report_date, content_sha256
            ) DO NOTHING
            """,
            (
                source_report_id,
                scope.namespace_id,
                work.get("provider_account_id"),
                provider_device_key,
                report_date,
                content_sha256,
                loaded["raw_ingress_record_id"],
                bool(normalized["no_data"]),
                loaded["received_at"],
                scope.namespace_id,
                work.get("provider_account_id"),
                provider_device_key,
                report_date,
            ),
        )


class PerceptorLiveNormalizationDispatcher:
    """Route live claims by durable work metadata on the existing queue."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        cipher: RawPayloadCipher,
    ) -> None:
        self.uow_factory = uow_factory
        self.cipher = cipher
        self._push: Any | None = None
        self._pull: PerceptorPullNormalizationProcessor | None = None

    def process(self, scope: UowScope, lease: NormalizationLease) -> Any:
        normalizer = self._normalizer(scope, lease)
        if normalizer == "perceptor_push":
            if self._push is None:
                from sleepagent.integrations.perceptor.ingestion import (
                    PerceptorNormalizationProcessor,
                )

                self._push = PerceptorNormalizationProcessor(
                    self.uow_factory, cipher=self.cipher
                )
            return self._push.process(scope, lease)
        if normalizer == PULL_NORMALIZER:
            if self._pull is None:
                self._pull = PerceptorPullNormalizationProcessor(
                    self.uow_factory, cipher=self.cipher
                )
            return self._pull.process(scope, lease)
        raise SleepSliceInvariantError("unsupported live normalization work kind")

    def _normalizer(self, scope: UowScope, lease: NormalizationLease) -> str:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT work_json ->> 'normalizer'
                    FROM public.sleep_domain_normalization_work
                    WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
                      AND namespace_generation = %s AND subject_id = %s
                      AND status = 'running' AND lease_generation = %s
                      AND fencing_token = %s AND worker_instance = %s
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (
                        lease.work_id,
                        scope.namespace_id,
                        scope.namespace_generation,
                        scope.subject_id,
                        lease.lease_generation,
                        lease.fencing_token,
                        lease.worker_instance,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise SleepSliceLeaseLost("live normalization dispatch fence was rejected")
        return str(row[0])


def parse_platform_success_response(raw_body: bytes) -> object:
    """Strictly parse the exact successful Platform envelope."""

    try:
        text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PerceptorPullIngressError("Platform response is not UTF-8") from exc

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PerceptorPullIngressError("Platform response has duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise PerceptorPullIngressError("Platform response has non-finite JSON")

    try:
        envelope = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise PerceptorPullIngressError("Platform response is malformed JSON") from exc
    if not isinstance(envelope, Mapping):
        raise PerceptorPullIngressError("Platform response envelope must be an object")
    _reject_non_finite_json(envelope)
    if envelope.get("success") is not True or str(envelope.get("code")) != "200":
        raise PerceptorPullIngressError("Platform response is not successful")
    if "data" not in envelope or envelope["data"] is None:
        raise PerceptorPullIngressError("Platform response data is missing")
    return envelope["data"]


def perceptor_pull_raw_aad(
    namespace_id: str,
    namespace_generation: int,
    raw_ingress_record_id: str,
) -> bytes:
    if (
        not namespace_id
        or namespace_generation < 1
        or not raw_ingress_record_id
        or "\0" in namespace_id
        or "\0" in raw_ingress_record_id
    ):
        raise ValueError("Pull raw AAD requires exact durable scope")
    return "\0".join(
        (
            "perceptor-pull-raw.v1",
            namespace_id,
            str(namespace_generation),
            raw_ingress_record_id,
        )
    ).encode("utf-8")


def _response_has_data(endpoint: str, data: object) -> bool:
    if endpoint == HISTORY_ENDPOINT:
        if not isinstance(data, list):
            raise PullContractError("history data must be a list")
        return bool(data)
    if not isinstance(data, Mapping):
        raise PullContractError("snapshot/report data must be an object")
    if endpoint == SLEEP_REPORT_ENDPOINT:
        return not sleep_report_is_no_data(data)
    return bool(data)


def _response_device_identity(
    endpoint: str,
    data: object,
    binding: DeviceBinding,
) -> tuple[str | None, str | None]:
    if endpoint != HISTORY_ENDPOINT:
        return None, None
    if not isinstance(data, list):
        raise PullContractError("history data must be a list")
    if not data:
        return None, None
    if any(
        not isinstance(item, Mapping)
        or item.get("device_id") in (None, "")
        or not str(item.get("device_id")).strip()
        for item in data
    ):
        return "__response_device_mismatch__", None
    identifiers = {str(item["device_id"]).strip() for item in data}
    if len(identifiers) != 1:
        return "__response_device_mismatch__", None
    identifier = identifiers.pop()
    if identifier == binding.provider_device.provider_device_id:
        return identifier, None
    if identifier == binding.provider_device.provider_device_name:
        return None, identifier
    return "__response_device_mismatch__", None


def _checkpoint_coordinates(
    binding: DeviceBinding,
    coordinates: PullRequestCoordinates,
    received_at: datetime,
) -> tuple[datetime, datetime]:
    if coordinates.endpoint == HISTORY_ENDPOINT:
        assert coordinates.window_end_at is not None
        cursor = coordinates.window_end_at
        return cursor, cursor - PULL_HISTORY_OVERLAP
    if coordinates.endpoint == SLEEP_REPORT_ENDPOINT:
        assert coordinates.report_date is not None
        next_local_midnight = datetime.combine(
            coordinates.report_date + timedelta(days=1),
            time.min,
            tzinfo=ZoneInfo(binding.timezone_name),
        )
        cursor = next_local_midnight.astimezone(UTC)
        return cursor, cursor
    return received_at, received_at


def _assert_pull_coordinates_within_binding(
    binding: DeviceBinding,
    coordinates: PullRequestCoordinates,
    *,
    requested_at: datetime,
) -> None:
    """Reject subject attribution unless the complete requested period is bound."""

    if coordinates.endpoint == HISTORY_ENDPOINT:
        assert coordinates.window_start_at is not None
        assert coordinates.window_end_at is not None
        period_start = coordinates.window_start_at
        period_end = coordinates.window_end_at
        end_is_inclusive = True
    elif coordinates.endpoint == SLEEP_REPORT_ENDPOINT:
        assert coordinates.report_date is not None
        zone = ZoneInfo(binding.timezone_name)
        period_start = datetime.combine(
            coordinates.report_date,
            time.min,
            tzinfo=zone,
        )
        period_end = datetime.combine(
            coordinates.report_date + timedelta(days=1),
            time.min,
            tzinfo=zone,
        )
        end_is_inclusive = False
    else:
        period_start = requested_at
        period_end = requested_at
        end_is_inclusive = True

    if period_start < binding.effective_from:
        raise ValueError("Pull data period precedes DeviceBinding effective interval")
    if binding.effective_until is None:
        return
    if period_end > binding.effective_until or (
        end_is_inclusive and period_end == binding.effective_until
    ):
        raise ValueError("Pull data period exceeds DeviceBinding effective interval")


def _request_descriptor(
    namespace: str,
    namespace_generation: int,
    account: str,
    binding: DeviceBinding,
    coordinates: PullRequestCoordinates,
) -> str:
    return json.dumps(
        [
            namespace,
            str(namespace_generation),
            account,
            binding.device_binding_id,
            str(binding.binding_version),
            coordinates.endpoint,
            _iso_or_none(coordinates.window_start_at) or "",
            _iso_or_none(coordinates.window_end_at) or "",
            "" if coordinates.report_date is None else coordinates.report_date.isoformat(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _stream_key(
    binding: DeviceBinding,
    endpoint: str,
    *,
    namespace_generation: int,
) -> str:
    surface = {
        HISTORY_ENDPOINT: "history.v1",
        REALTIME_READ_ENDPOINT: "realtime.v1",
        GET_CURRENT_ENDPOINT: "current.v1",
        SLEEP_REPORT_ENDPOINT: "sleep_report.v1",
    }[endpoint]
    return "perceptor:pull:stream:" + _stable_digest(
        binding.provider_account_id,
        binding.device_binding_id,
        str(binding.binding_version),
        str(namespace_generation),
        surface,
    )


def _provider_device_key(
    binding: DeviceBinding,
    *,
    namespace_generation: int,
) -> str:
    value = (
        binding.provider_device.provider_device_id
        or binding.provider_device.provider_device_name
        or ""
    )
    material = f"{namespace_generation}\x1f{value}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _emit_reconciliation_observability(
    work: Mapping[str, Any],
    summary: Mapping[str, Any],
    checkpoint_advanced: bool,
) -> None:
    endpoint = work.get("endpoint")
    if summary.get("no_data"):
        record_backend_signal(category="pull", outcome="no_data")
        log_event("pull_no_data", endpoint=endpoint)
    for event, key, outcome in (
        ("pull_backfill_created", "canonical_created_count", "backfill_created"),
        ("push_pull_overlap", "push_pull_overlap_count", "overlap"),
        ("push_pull_conflict", "conflict_created_count", "conflict"),
    ):
        count = int(summary.get(key, 0))
        if count:
            record_backend_signal(category="reconciliation", outcome=outcome)
            log_event(event, endpoint=endpoint, count=count)
    duplicate_count = int(summary.get("duplicate_count", 0))
    if duplicate_count:
        record_backend_signal(category="pull", outcome="duplicate")
        log_event("pull_duplicate", endpoint=endpoint, count=duplicate_count)
    checkpoint_outcome = "checkpoint_advanced" if checkpoint_advanced else "checkpoint_held"
    record_backend_signal(category="pull", outcome=checkpoint_outcome)
    log_event(f"pull_{checkpoint_outcome}", endpoint=endpoint)


def _phase_receipt_id(work_id: str) -> str:
    return _stable_prefixed_id("receipt", work_id, "reconciled")


def _stable_prefixed_id(prefix: str, *parts: object) -> str:
    return f"perceptor:pull:{prefix}:" + _stable_digest(*parts)


def _stable_digest(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _semantic_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _reject_non_finite_json(value: object) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PerceptorPullIngressError("Platform response has non-finite JSON")
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_non_finite_json(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_non_finite_json(nested)


def _aware_from_text(value: object, name: str) -> datetime:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SleepSliceInvariantError(f"{name} is invalid") from exc
    _require_aware(parsed, name)
    return parsed


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PerceptorPullIngressError(f"{name} is not a timestamp")
    _require_aware(value, name)
    return value.astimezone(UTC)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SleepSliceInvariantError(f"{name} is required")
    return value


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _json_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise SleepSliceInvariantError("persisted JSON value is not an object")
    return dict(value)


__all__ = [
    "DurablePerceptorPullIngress",
    "PULL_ENDPOINTS",
    "PULL_HISTORY_OVERLAP",
    "PULL_NORMALIZER",
    "PULL_RESPONSE_PROFILE",
    "PerceptorLiveNormalizationDispatcher",
    "PerceptorPullBackfillRunner",
    "PerceptorPullIngressError",
    "PerceptorPullIngressResult",
    "PerceptorPullNormalizationProcessor",
    "PerceptorPullNormalizationResult",
    "PullHistoryPlan",
    "PullRequestCoordinates",
    "parse_platform_success_response",
    "perceptor_pull_raw_aad",
]
