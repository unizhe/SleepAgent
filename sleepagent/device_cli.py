"""Supported CLI for governed DeviceBinding and acquisition schedule management."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from typing import Any

from sleepagent.application.acquisition import (
    AcquisitionJobType,
    AcquisitionScheduleService,
)
from sleepagent.application.device_bindings import DeviceBindingService
from sleepagent.config import (
    BackendKeyProvider,
    DataMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.domain.contracts import ProviderDeviceIdentity
from sleepagent.integrations.perceptor.client import PerceptorPlatformClient
from sleepagent.integrations.perceptor.pull_ingestion import (
    DurablePerceptorPullIngress,
    PerceptorPullBackfillRunner,
    PerceptorPullQuarantineReprocessor,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sleepagent-device")
    parser.add_argument("--namespace-id", required=True)
    parser.add_argument("--namespace-generation", type=int, required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument(
        "--actor-role",
        choices=("elder", "family", "doctor"),
        required=True,
    )
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--authorization-epoch", type=int, required=True)
    parser.add_argument("--privacy-epoch", type=int, required=True)
    parser.add_argument("--retrieval-policy-epoch", type=int, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--arm-id")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover")
    discover.add_argument("--provider-id", default="perceptor")
    discover.add_argument("--provider-account-id", required=True)

    listing = commands.add_parser("list")
    listing.add_argument("--device-id")
    listing.add_argument("--active-only", action="store_true")

    show = commands.add_parser("show")
    show.add_argument("device_binding_id")

    for name in ("bind", "rebind"):
        command = commands.add_parser(name)
        if name == "rebind":
            command.add_argument("current_binding_id")
            command.add_argument("--expected-cas", type=int, required=True)
        else:
            command.add_argument("--device-id", required=True)
            command.add_argument("--provider-id", default="perceptor")
            command.add_argument("--provider-account-id", required=True)
            command.add_argument("--provider-device-id")
            command.add_argument("--provider-device-name")
            command.add_argument("--home-id")
        command.add_argument("--command-id", required=True)
        command.add_argument("--timezone-name", required=True)
        command.add_argument("--effective-at", required=True)
        command.add_argument("--reason", required=True)

    for name in ("end", "unbind", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("device_binding_id")
        command.add_argument("--expected-cas", type=int, required=True)
        command.add_argument("--command-id", required=True)
        command.add_argument("--effective-at", required=True)
        command.add_argument("--reason", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("device_binding_id")

    schedule = commands.add_parser("schedule-create")
    schedule.add_argument("device_binding_id")
    schedule.add_argument(
        "--job-type",
        choices=tuple(item.value for item in AcquisitionJobType),
        required=True,
    )
    schedule.add_argument("--next-run-at", required=True)
    schedule.add_argument("--cadence-seconds", type=int, required=True)
    schedule.add_argument("--jitter-seconds", type=int, default=0)

    commands.add_parser("schedule-list")
    for name in ("schedule-pause", "schedule-resume"):
        command = commands.add_parser(name)
        command.add_argument("schedule_id")
        command.add_argument("--expected-cas", type=int, required=True)
        if name == "schedule-resume":
            command.add_argument("--next-run-at")
    history_backfill = commands.add_parser("history-backfill")
    history_backfill.add_argument("device_binding_id")
    history_backfill.add_argument("--start-at", required=True)
    history_backfill.add_argument("--end-at", required=True)

    sleep_report = commands.add_parser("sleep-report-recover")
    sleep_report.add_argument("device_binding_id")
    sleep_report.add_argument("--report-date", required=True)
    quarantine_reprocess = commands.add_parser("pull-quarantine-reprocess")
    quarantine_reprocess.add_argument("quarantine_id")
    quarantine_reprocess.add_argument("--expected-work-id", required=True)
    quarantine_reprocess.add_argument("--request-id", required=True)
    quarantine_reprocess.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = SleepBackendSettings.from_environment()
    recovery_command = arguments.command in {
        "history-backfill",
        "sleep-report-recover",
    }
    required_role = ProcessRole.WORKER if recovery_command else ProcessRole.API
    if settings.process_role is not required_role:
        raise ValueError(
            "Perceptor recovery requires a Worker capability profile"
            if recovery_command
            else "DeviceBinding CLI requires an API capability profile"
        )
    scope = UowScope(
        namespace_id=arguments.namespace_id,
        data_mode=settings.data_mode.value,  # type: ignore[arg-type]
        process_role="worker" if recovery_command else "api",
        purpose="worker" if recovery_command else "device_binding_management",
        service_principal_id=settings.service_principal_id,
        namespace_generation=arguments.namespace_generation,
        subject_id=arguments.subject_id,
        actor_id=None if recovery_command else arguments.actor_id,
        actor_role=None if recovery_command else arguments.actor_role,
        run_id=arguments.run_id,
        arm_id=arguments.arm_id,
        authorization_epoch=arguments.authorization_epoch,
        privacy_epoch=arguments.privacy_epoch,
        retrieval_policy_epoch=arguments.retrieval_policy_epoch,
        worker_instance="perceptor-recovery-cli" if recovery_command else None,
    )
    pool = PsycopgPoolProvider.from_dsn(
        settings.database_dsn.get_secret_value(),
        configuration=PoolConfiguration(
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open_timeout_seconds=settings.pool_timeout_seconds,
        ),
        application_name="sleepagent-device-cli",
    )
    uow_factory = UnitOfWorkFactory(
        pool,
        lock_timeout_ms=settings.lock_timeout_ms,
        statement_timeout_ms=settings.statement_timeout_ms,
        idle_in_transaction_timeout_ms=settings.idle_transaction_timeout_ms,
    )
    pool.open()
    try:
        result = _execute(
            arguments,
            scope=scope,
            bindings=DeviceBindingService(uow_factory),
            schedules=AcquisitionScheduleService(uow_factory),
            settings=settings,
            uow_factory=uow_factory,
        )
    finally:
        pool.close()
    print(json.dumps(_jsonable(result), ensure_ascii=False, sort_keys=True))
    return 0


def _execute(
    arguments: argparse.Namespace,
    *,
    scope: UowScope,
    bindings: DeviceBindingService,
    schedules: AcquisitionScheduleService,
    settings: SleepBackendSettings | None = None,
    uow_factory: UnitOfWorkFactory[Any] | None = None,
) -> Any:
    command = arguments.command
    if command == "discover":
        return bindings.discover(
            scope,
            provider_id=arguments.provider_id,
            provider_account_id=arguments.provider_account_id,
        )
    if command == "list":
        return bindings.list_bindings(
            scope,
            device_id=arguments.device_id,
            include_inactive=not arguments.active_only,
        )
    if command == "show":
        return bindings.show(scope, device_binding_id=arguments.device_binding_id)
    if command == "bind":
        return bindings.bind(
            scope,
            command_id=arguments.command_id,
            device_id=arguments.device_id,
            provider_id=arguments.provider_id,
            provider_account_id=arguments.provider_account_id,
            provider_device=_provider_device(arguments),
            subject_id=arguments.subject_id,
            timezone_name=arguments.timezone_name,
            effective_at=_instant(arguments.effective_at),
            actor_id=arguments.actor_id,
            authorization_id=arguments.authorization_id,
            reason=arguments.reason,
        )
    if command == "rebind":
        current = bindings.show(scope, device_binding_id=arguments.current_binding_id)
        current = replace(current, cas_version=arguments.expected_cas)
        return bindings.rebind(
            scope,
            current=current,
            command_id=arguments.command_id,
            subject_id=arguments.subject_id,
            timezone_name=arguments.timezone_name,
            effective_at=_instant(arguments.effective_at),
            actor_id=arguments.actor_id,
            authorization_id=arguments.authorization_id,
            reason=arguments.reason,
        )
    if command in {"end", "unbind", "revoke"}:
        current = bindings.show(scope, device_binding_id=arguments.device_binding_id)
        current = replace(current, cas_version=arguments.expected_cas)
        method = bindings.revoke if command == "revoke" else bindings.end
        return method(
            scope,
            current=current,
            command_id=arguments.command_id,
            effective_at=_instant(arguments.effective_at),
            actor_id=arguments.actor_id,
            authorization_id=arguments.authorization_id,
            reason=arguments.reason,
        )
    if command == "validate":
        return bindings.validate(scope, device_binding_id=arguments.device_binding_id)
    if command == "schedule-create":
        binding = bindings.show(scope, device_binding_id=arguments.device_binding_id)
        return schedules.create(
            scope,
            binding=binding,
            job_type=AcquisitionJobType(arguments.job_type),
            next_run_at=_instant(arguments.next_run_at),
            cadence_seconds=arguments.cadence_seconds,
            jitter_seconds=arguments.jitter_seconds,
        )
    if command == "schedule-list":
        return schedules.list(scope)
    if command == "schedule-pause":
        return schedules.pause(
            scope,
            schedule_id=arguments.schedule_id,
            expected_cas=arguments.expected_cas,
        )
    if command == "schedule-resume":
        return schedules.resume(
            scope,
            schedule_id=arguments.schedule_id,
            expected_cas=arguments.expected_cas,
            next_run_at=(
                None
                if arguments.next_run_at is None
                else _instant(arguments.next_run_at)
            ),
        )
    if command == "pull-quarantine-reprocess":
        if uow_factory is None:
            raise ValueError("Pull quarantine reprocessing requires persistence")
        return PerceptorPullQuarantineReprocessor(uow_factory).requeue(
            scope,
            quarantine_id=arguments.quarantine_id,
            expected_work_id=arguments.expected_work_id,
            request_id=arguments.request_id,
            actor_id=arguments.actor_id,
            authorization_id=arguments.authorization_id,
            reason=arguments.reason,
        )
    if command in {"history-backfill", "sleep-report-recover"}:
        if settings is None or uow_factory is None:
            raise ValueError("Perceptor recovery requires configured persistence")
        binding_view = bindings.show(
            scope, device_binding_id=arguments.device_binding_id
        )
        binding = binding_view.binding
        _validate_recovery_authority(settings, scope, binding)
        client_id = _perceptor_secret(
            settings, settings.perceptor_client_id_ref, "Perceptor client ID"
        )
        client_secret = _perceptor_secret(
            settings,
            settings.perceptor_client_secret_ref,
            "Perceptor historical recovery",
        )
        ingress = DurablePerceptorPullIngress(
            settings,
            uow_factory,
            client_id_sha256=hashlib.sha256(client_id.encode("utf-8")).hexdigest(),
            operation_scope=scope,
        )
        with PerceptorPlatformClient(
            client_id=client_id,
            client_secret=client_secret,
            base_url=settings.perceptor_base_url,
        ) as client:
            runner = PerceptorPullBackfillRunner(client, ingress, binding)
            if command == "history-backfill":
                return runner.backfill_history(
                    start_at=_instant(arguments.start_at),
                    end_at=_instant(arguments.end_at),
                    normalization_delay_seconds=600,
                )
            report_date = date.fromisoformat(arguments.report_date)
            return runner.pull_sleep_report(
                report_date,
                normalization_delay_seconds=600,
            )
    raise ValueError("unknown device command")


def _validate_recovery_authority(
    settings: SleepBackendSettings,
    scope: UowScope,
    binding: Any,
) -> None:
    if settings.data_mode is not DataMode.LIVE:
        raise ValueError("Perceptor recovery requires live data mode")
    if settings.provider_mode is not ProviderMode.LIVE:
        raise ValueError("Perceptor recovery requires live provider mode")
    if scope.namespace_id != settings.perceptor_namespace_id:
        raise ValueError("recovery namespace does not match Perceptor authority")
    if scope.namespace_generation != settings.perceptor_namespace_generation:
        raise ValueError("recovery namespace generation does not match authority")
    if scope.subject_id != binding.subject_id:
        raise ValueError("recovery subject does not match DeviceBinding")
    if binding.data_mode.value != "live" or binding.status.value != "active":
        raise ValueError("recovery requires an active live DeviceBinding")
    if binding.provider_id != "perceptor":
        raise ValueError("recovery requires a Perceptor DeviceBinding")
    if binding.provider_account_id != settings.perceptor_provider_account_id:
        raise ValueError("recovery provider account does not match authority")


def _perceptor_secret(
    settings: SleepBackendSettings,
    reference: str | None,
    purpose: str,
) -> str:
    if reference is None:
        raise ValueError(f"{purpose} is not configured")
    return BackendKeyProvider(settings.deployment_mode).secret(
        reference,
        purpose=purpose,
        minimum_bytes=1,
    ).decode("utf-8", errors="strict")


def _provider_device(arguments: argparse.Namespace) -> ProviderDeviceIdentity:
    return ProviderDeviceIdentity(
        provider_device_id=arguments.provider_device_id,
        provider_device_name=arguments.provider_device_name,
        home_id=arguments.home_id,
    )


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return instant


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "binding"):
        return {
            "binding": value.binding.model_dump(mode="json"),
            "cas_version": value.cas_version,
        }
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
