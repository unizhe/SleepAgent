"""Concrete Perceptor executor for provider-neutral scheduled acquisition work."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sleepagent.application.acquisition import AcquisitionJobType
from sleepagent.application.device_bindings import DeviceBindingService
from sleepagent.application.night_finalization import NightFinalizationService
from sleepagent.config import BackendKeyProvider, SleepBackendSettings
from sleepagent.integrations.perceptor.client import PerceptorPlatformClient
from sleepagent.integrations.perceptor.pull_ingestion import (
    DurablePerceptorPullIngress,
    PerceptorPullBackfillRunner,
)
from sleepagent.persistence.uow import UnitOfWorkFactory
from sleepagent.workers.kernel import WorkContext
from sleepagent.workers.runtime import B3ClaimInvariantError, exact_worker_scope


class PerceptorAcquisitionExecutor:
    """Reuse the existing read-only Perceptor Pull and intake authorities."""

    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        client: PerceptorPlatformClient | None = None,
    ) -> None:
        self.settings = settings
        self.uow_factory = uow_factory
        self._client = client

    def execute(
        self,
        *,
        context: WorkContext,
        job_type: AcquisitionJobType,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        scope = exact_worker_scope(context, allowed_handler=job_type.value)
        instant = datetime.fromisoformat(str(payload["scheduled_for"]))
        if job_type is AcquisitionJobType.NIGHT_FINALIZATION_SCAN:
            result = NightFinalizationService(
                self.uow_factory
            ).finalize_latest_for_binding(
                scope,
                device_binding_id=str(payload["device_binding_id"]),
                evaluated_at=instant,
            )
            return result.model_dump(mode="json")

        binding = DeviceBindingService(self.uow_factory).show(
            scope,
            device_binding_id=str(payload["device_binding_id"]),
        )
        if binding.binding.binding_version != int(payload["binding_version"]):
            raise B3ClaimInvariantError("scheduled work binding version drifted")
        client = self._client or self._build_client()
        ingress = DurablePerceptorPullIngress(
            self.settings,
            self.uow_factory,
            client_id_sha256=hashlib.sha256(
                self._client_id().encode("utf-8")
            ).hexdigest(),
        )
        runner = PerceptorPullBackfillRunner(client, ingress, binding.binding)
        if job_type is AcquisitionJobType.HISTORY_OVERLAP_PULL:
            result = runner.pull_history(
                start_at=instant - timedelta(minutes=15),
                end_at=instant,
            )
        else:
            report_date = instant.astimezone(
                ZoneInfo(binding.binding.timezone_name)
            ).date()
            result = runner.pull_sleep_report(report_date)
        return {
            "disposition": result.disposition,
            "raw_ingress_record_id": result.raw_ingress_record_id,
            "normalization_work_id": result.normalization_work_id,
            "duplicate": result.duplicate,
        }

    def _client_id(self) -> str:
        reference = self.settings.perceptor_client_id_ref
        if reference is None:
            raise ValueError("Perceptor client ID is not configured")
        raw = BackendKeyProvider(self.settings.deployment_mode).secret(
            reference,
            purpose="Perceptor client ID",
            minimum_bytes=1,
        )
        return raw.decode("utf-8", errors="strict")

    def _build_client(self) -> PerceptorPlatformClient:
        reference = self.settings.perceptor_client_secret_ref
        if reference is None:
            raise ValueError("Perceptor client secret is not configured")
        secret = BackendKeyProvider(self.settings.deployment_mode).secret(
            reference,
            purpose="Perceptor scheduled Pull",
            minimum_bytes=1,
        ).decode("utf-8", errors="strict")
        self._client = PerceptorPlatformClient(
            client_id=self._client_id(),
            client_secret=secret,
        )
        return self._client


__all__ = ["PerceptorAcquisitionExecutor"]
