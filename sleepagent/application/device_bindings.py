"""Governed application service for temporal DeviceBinding management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.domain.contracts import (
    DataMode,
    DeviceBinding,
    DeviceBindingStatus,
    ProviderDeviceIdentity,
)
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope


UTC = timezone.utc
BindingAction = Literal["created", "rebound", "ended", "revoked", "validated"]


class DeviceBindingConflict(RuntimeError):
    """A temporal, identity, or optimistic-version invariant was rejected."""


class DeviceBindingNotFound(KeyError):
    pass


class DiscoveredDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1)
    provider_account_id: str = Field(min_length=1)
    provider_device_key: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    provider_device: ProviderDeviceIdentity
    active_binding_id: str | None = None
    active_subject_id: str | None = None


class DeviceBindingValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device_binding_id: str
    valid: bool
    checks: tuple[str, ...]
    binding: DeviceBinding
    cas_version: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class ManagedDeviceBinding:
    binding: DeviceBinding
    cas_version: int


class DeviceBindingService:
    """One RLS-scoped service for discover/list/bind/rebind/end/revoke/validate."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        now_factory: Any = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.uow_factory = uow_factory
        self.now_factory = now_factory

    def discover(
        self,
        scope: UowScope,
        *,
        provider_id: str,
        provider_account_id: str,
    ) -> tuple[DiscoveredDevice, ...]:
        """List durable provider discoveries; this never contacts a device."""

        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT identity.provider_device_key, identity.device_id,
                      identity.provider_device_json,
                      binding.device_binding_id, binding.subject_id
                    FROM public.sleep_domain_device_identities AS identity
                    LEFT JOIN public.sleep_domain_device_bindings AS binding
                      ON binding.namespace_id = identity.namespace_id
                     AND binding.data_mode = identity.data_mode
                     AND binding.device_id = identity.device_id
                     AND binding.status = 'active'
                    WHERE identity.namespace_id = %s
                      AND identity.data_mode = %s
                      AND identity.provider_id = %s
                      AND identity.provider_account_id = %s
                    ORDER BY identity.provider_device_key
                    """,
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        provider_id,
                        provider_account_id,
                    ),
                )
                rows = tuple(cursor.fetchall())
            finally:
                cursor.close()
        return tuple(
            DiscoveredDevice(
                provider_id=provider_id,
                provider_account_id=provider_account_id,
                provider_device_key=str(row[0]),
                device_id=str(row[1]),
                provider_device=_provider_device(row[2]),
                active_binding_id=None if row[3] is None else str(row[3]),
                active_subject_id=None if row[4] is None else str(row[4]),
            )
            for row in rows
        )

    def list_bindings(
        self,
        scope: UowScope,
        *,
        device_id: str | None = None,
        include_inactive: bool = True,
    ) -> tuple[ManagedDeviceBinding, ...]:
        clauses = ["binding.namespace_id = %s", "binding.data_mode = %s"]
        parameters: list[Any] = [scope.namespace_id, scope.data_mode]
        if scope.subject_id is not None:
            clauses.append("binding.subject_id = %s")
            parameters.append(scope.subject_id)
        if device_id is not None:
            clauses.append("binding.device_id = %s")
            parameters.append(device_id)
        if not include_inactive:
            clauses.append("binding.status = 'active'")
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _binding_select()
                    + " WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY binding.device_id, binding.binding_version",
                    tuple(parameters),
                )
                rows = tuple(cursor.fetchall())
            finally:
                cursor.close()
        return tuple(_managed_binding(row) for row in rows)

    def show(
        self,
        scope: UowScope,
        *,
        device_binding_id: str,
    ) -> ManagedDeviceBinding:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _binding_select()
                    + " WHERE binding.device_binding_id = %s"
                    + " AND binding.namespace_id = %s AND binding.data_mode = %s",
                    (device_binding_id, scope.namespace_id, scope.data_mode),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise DeviceBindingNotFound(device_binding_id)
        return _managed_binding(row)

    def bind(
        self,
        scope: UowScope,
        *,
        command_id: str,
        device_id: str,
        provider_id: str,
        provider_account_id: str,
        provider_device: ProviderDeviceIdentity,
        subject_id: str,
        timezone_name: str,
        effective_at: datetime,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        _validate_scope_command(scope, subject_id=subject_id, actor_id=actor_id)
        _validate_timezone(timezone_name)
        binding_id = _identifier("binding", scope.namespace_id, command_id)
        binding = DeviceBinding(
            data_mode=DataMode(scope.data_mode),
            device_binding_id=binding_id,
            binding_version=1,
            device_id=device_id,
            provider_id=provider_id,
            provider_account_id=provider_account_id,
            provider_device=provider_device,
            subject_id=subject_id,
            timezone_name=timezone_name,
            effective_from=effective_at,
            status=DeviceBindingStatus.ACTIVE,
            changed_by_actor_id=actor_id,
            change_reason=reason,
            recorded_at=self.now_factory(),
        )
        return self._manage(
            scope,
            action="created",
            command_id=command_id,
            current=None,
            new_binding=binding,
            actor_id=actor_id,
            authorization_id=authorization_id,
            reason=reason,
        )

    def rebind(
        self,
        scope: UowScope,
        *,
        current: ManagedDeviceBinding,
        command_id: str,
        subject_id: str,
        timezone_name: str,
        effective_at: datetime,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        _validate_scope_command(scope, subject_id=subject_id, actor_id=actor_id)
        _validate_timezone(timezone_name)
        old = current.binding
        binding_id = _identifier("binding", scope.namespace_id, command_id)
        new = DeviceBinding(
            **{
                **old.model_dump(mode="python"),
                "device_binding_id": binding_id,
                "binding_version": old.binding_version + 1,
                "subject_id": subject_id,
                "timezone_name": timezone_name,
                "effective_from": effective_at,
                "effective_until": None,
                "status": DeviceBindingStatus.ACTIVE,
                "changed_by_actor_id": actor_id,
                "change_reason": reason,
                "recorded_at": self.now_factory(),
            }
        )
        return self._manage(
            scope,
            action="rebound",
            command_id=command_id,
            current=current,
            new_binding=new,
            actor_id=actor_id,
            authorization_id=authorization_id,
            reason=reason,
        )

    def end(
        self,
        scope: UowScope,
        *,
        current: ManagedDeviceBinding,
        command_id: str,
        effective_at: datetime,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        return self._close(
            scope,
            action="ended",
            current=current,
            command_id=command_id,
            effective_at=effective_at,
            actor_id=actor_id,
            authorization_id=authorization_id,
            reason=reason,
        )

    unbind = end

    def revoke(
        self,
        scope: UowScope,
        *,
        current: ManagedDeviceBinding,
        command_id: str,
        effective_at: datetime,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        return self._close(
            scope,
            action="revoked",
            current=current,
            command_id=command_id,
            effective_at=effective_at,
            actor_id=actor_id,
            authorization_id=authorization_id,
            reason=reason,
        )

    def validate(
        self,
        scope: UowScope,
        *,
        device_binding_id: str,
    ) -> DeviceBindingValidation:
        managed = self.show(scope, device_binding_id=device_binding_id)
        binding = managed.binding
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.sleep_domain_device_bindings AS other
                    WHERE other.namespace_id = %s AND other.data_mode = %s
                      AND other.device_id = %s
                      AND other.device_binding_id <> %s
                      AND tstzrange(other.effective_from, other.effective_until, '[)')
                          && tstzrange(%s, %s, '[)')
                    """,
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        binding.device_id,
                        binding.device_binding_id,
                        binding.effective_from,
                        binding.effective_until,
                    ),
                )
                overlap_count = int(cursor.fetchone()[0])
            finally:
                cursor.close()
        checks = (
            "iana_timezone_valid",
            "temporal_interval_valid",
            "provider_identity_bound",
            "no_overlapping_device_interval",
            "optimistic_version_present",
        )
        return DeviceBindingValidation(
            device_binding_id=device_binding_id,
            valid=overlap_count == 0,
            checks=checks,
            binding=binding,
            cas_version=managed.cas_version,
        )

    def _close(
        self,
        scope: UowScope,
        *,
        action: Literal["ended", "revoked"],
        current: ManagedDeviceBinding,
        command_id: str,
        effective_at: datetime,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        old = current.binding
        _validate_scope_command(scope, subject_id=old.subject_id, actor_id=actor_id)
        status = (
            DeviceBindingStatus.ENDED
            if action == "ended"
            else DeviceBindingStatus.REVOKED
        )
        closed = DeviceBinding(
            **{
                **old.model_dump(mode="python"),
                "effective_until": effective_at,
                "status": status,
                "changed_by_actor_id": actor_id,
                "change_reason": reason,
                "recorded_at": self.now_factory(),
            }
        )
        return self._manage(
            scope,
            action=action,
            command_id=command_id,
            current=current,
            new_binding=closed,
            actor_id=actor_id,
            authorization_id=authorization_id,
            reason=reason,
        )

    def _manage(
        self,
        scope: UowScope,
        *,
        action: BindingAction,
        command_id: str,
        current: ManagedDeviceBinding | None,
        new_binding: DeviceBinding,
        actor_id: str,
        authorization_id: str,
        reason: str,
    ) -> ManagedDeviceBinding:
        provider_device = new_binding.provider_device
        provider_key = _provider_device_key(provider_device)
        command_material = {
            "action": action,
            "current_device_binding_id": (
                None if current is None else current.binding.device_binding_id
            ),
            "expected_cas": None if current is None else current.cas_version,
            "new_device_binding_id": new_binding.device_binding_id,
            "device_id": new_binding.device_id,
            "provider_id": new_binding.provider_id,
            "provider_account_id": new_binding.provider_account_id,
            "provider_device_key": provider_key,
            "provider_device": provider_device.model_dump(mode="json"),
            "subject_id": new_binding.subject_id,
            "timezone_name": new_binding.timezone_name,
            "effective_at": (
                new_binding.effective_from
                if action in {"created", "rebound"}
                else new_binding.effective_until
            ),
            "actor_id": actor_id,
            "authorization_id": authorization_id,
            "reason": reason,
        }
        command_canonical = json.dumps(
            command_material,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        audit = {
            "schema_version": "device_binding_audit_event.v2",
            "command_id": command_id,
            "action": action,
            "actor_id": actor_id,
            "authorization_id": authorization_id,
            "reason": reason,
            "command_sha256": hashlib.sha256(
                command_canonical.encode()
            ).hexdigest(),
            "previous_device_binding_id": (
                None if current is None else current.binding.device_binding_id
            ),
            "new_device_binding_id": new_binding.device_binding_id,
        }
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_manage_device_binding("
                    + ",".join(["%s"] * 18)
                    + ")",
                    (
                        command_id,
                        action,
                        None if current is None else current.binding.device_binding_id,
                        None if current is None else current.cas_version,
                        new_binding.device_binding_id,
                        new_binding.device_id,
                        new_binding.provider_id,
                        new_binding.provider_account_id,
                        provider_key,
                        json.dumps(provider_device.model_dump(mode="json")),
                        new_binding.subject_id,
                        new_binding.timezone_name,
                        new_binding.effective_from
                        if action == "created"
                        else (
                            new_binding.effective_from
                            if action == "rebound"
                            else new_binding.effective_until
                        ),
                        actor_id,
                        authorization_id,
                        reason,
                        new_binding.model_dump_json(),
                        json.dumps(audit, sort_keys=True, separators=(",", ":")),
                    ),
                )
                row = cursor.fetchone()
            except Exception as exc:
                raise DeviceBindingConflict(
                    "DeviceBinding command was rejected"
                ) from exc
            finally:
                cursor.close()
            if row is None:
                raise DeviceBindingConflict(
                    "DeviceBinding command returned no authority"
                )
            uow.commit()
        return self.show(scope, device_binding_id=str(row[0]))


def _binding_select() -> str:
    return """
        SELECT binding.device_binding_id, binding.data_mode,
          binding.binding_version, binding.device_id, binding.provider_id,
          binding.provider_account_id,
          binding.subject_id, binding.timezone_name, binding.effective_from,
          binding.effective_until, binding.status, binding.binding_json,
          binding.recorded_at, binding.cas_version,
          identity.provider_device_json
        FROM public.sleep_domain_device_bindings AS binding
        JOIN public.sleep_domain_device_identities AS identity
          ON identity.namespace_id = binding.namespace_id
         AND identity.data_mode = binding.data_mode
         AND identity.provider_id = binding.provider_id
         AND identity.provider_account_id = binding.provider_account_id
         AND identity.device_id = binding.device_id
    """


def _managed_binding(row: Any) -> ManagedDeviceBinding:
    serialized = _json_object(row[11])
    provider_device = _provider_device(row[14])
    binding = DeviceBinding.model_validate(
        {
            "schema_version": "device_binding.v1",
            "data_mode": str(row[1]),
            "device_binding_id": str(row[0]),
            "binding_version": int(row[2]),
            "device_id": str(row[3]),
            "provider_id": str(row[4]),
            "provider_account_id": str(row[5]),
            "provider_device": provider_device.model_dump(mode="python"),
            "subject_id": str(row[6]),
            "timezone_name": str(row[7]),
            "effective_from": row[8],
            "effective_until": row[9],
            "status": str(row[10]),
            "changed_by_actor_id": str(
                serialized.get("changed_by_actor_id") or "historical-system"
            ),
            "change_reason": str(
                serialized.get("change_reason") or "historical_import"
            ),
            "recorded_at": row[12],
        }
    )
    return ManagedDeviceBinding(binding=binding, cas_version=int(row[13]))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def _provider_device(value: Any) -> ProviderDeviceIdentity:
    payload = _json_object(value)
    if payload.get("schema_version") != "provider_device_identity.v1":
        payload = {
            "schema_version": "provider_device_identity.v1",
            "provider_device_id": payload.get("provider_device_id"),
            "provider_device_name": payload.get("provider_device_name"),
            "product_id": payload.get("product_id"),
            "home_id": payload.get("home_id"),
            "project_id": payload.get("project_id"),
            "native_keys": payload.get("native_keys") or {},
        }
    return ProviderDeviceIdentity.model_validate(payload)


def _provider_device_key(value: ProviderDeviceIdentity) -> str:
    native = value.provider_device_id or value.provider_device_name
    if native:
        return native
    material = json.dumps(
        value.native_keys, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"{prefix}:{digest}"


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone_name must be a valid IANA timezone") from exc


def _validate_scope_command(
    scope: UowScope, *, subject_id: str, actor_id: str
) -> None:
    if scope.process_role != "api" or scope.purpose != "device_binding_management":
        raise PermissionError("DeviceBinding management requires API actor authority")
    if scope.subject_id != subject_id or scope.actor_id != actor_id:
        raise PermissionError(
            "DeviceBinding command does not match its authority scope"
        )


__all__ = [
    "DeviceBindingConflict",
    "DeviceBindingNotFound",
    "DeviceBindingService",
    "DeviceBindingValidation",
    "DiscoveredDevice",
    "ManagedDeviceBinding",
]
