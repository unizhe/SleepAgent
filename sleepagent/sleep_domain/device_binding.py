"""Internal, authorized DeviceBinding command service.

This module is deliberately not an HTTP API.  It turns an explicitly scoped
administrative command into a versioned effective-time binding using repository
CAS and an immutable audit event.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal, Mapping

from pydantic import Field, model_validator

from sleepagent.sleep_domain.contracts import (
    DataMode,
    DeviceBinding,
    DeviceBindingAuditAction,
    DeviceBindingAuditEvent,
    DeviceBindingStatus,
    NonEmptyStr,
    ProviderDeviceIdentity,
    SleepDomainContract,
)
from sleepagent.sleep_domain.repository import (
    CasConflictError,
    DomainNamespace,
    SleepDomainRepository,
)


class AdministrativeScope(str, Enum):
    DEVICE_BINDING_WRITE = "device_binding:write"
    QUARANTINE_REPROCESS = "quarantine:reprocess"


class AdministrativeAuthorizationError(PermissionError):
    pass


class AdministrativeAccessPolicy:
    """Small internal policy boundary backed by deployment-owned grants."""

    def __init__(
        self,
        grants: Mapping[str, frozenset[AdministrativeScope]],
        *,
        authorization_ids: Mapping[str, frozenset[str]],
    ) -> None:
        self._grants = {
            actor_id: frozenset(scopes) for actor_id, scopes in grants.items()
        }
        self._authorization_ids = {
            actor_id: frozenset(ids)
            for actor_id, ids in authorization_ids.items()
        }

    def require(
        self,
        *,
        actor_id: str,
        authorization_id: str,
        scope: AdministrativeScope,
    ) -> None:
        if not actor_id or not authorization_id:
            raise AdministrativeAuthorizationError(
                "administrative actor and authorization proof are required"
            )
        if authorization_id not in self._authorization_ids.get(
            actor_id, frozenset()
        ):
            raise AdministrativeAuthorizationError(
                "administrative authorization proof is not recognized"
            )
        if scope not in self._grants.get(actor_id, frozenset()):
            raise AdministrativeAuthorizationError(
                f"actor is not authorized for {scope.value}"
            )


class DeviceBindingCommand(SleepDomainContract):
    schema_version: Literal["device_binding_command.v1"] = "device_binding_command.v1"
    command_id: NonEmptyStr
    data_mode: DataMode
    device_binding_id: NonEmptyStr
    expected_binding_version: int = Field(..., ge=0)
    device_id: NonEmptyStr
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    provider_device: ProviderDeviceIdentity
    subject_id: NonEmptyStr
    timezone_name: NonEmptyStr
    effective_from: datetime
    effective_until: datetime | None = None
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    change_reason: NonEmptyStr
    requested_at: datetime

    @model_validator(mode="before")
    @classmethod
    def require_explicit_effective_time(cls, values: object) -> object:
        if isinstance(values, dict) and values.get("effective_from") is None:
            raise ValueError("effective_from must be explicit")
        return values


def namespaced_provider_device_key(
    *,
    provider_id: str,
    provider_account_id: str,
    provider_device: ProviderDeviceIdentity,
) -> str:
    """Hash opaque provider identifiers without parsing or numeric coercion."""

    stable_identity = {
        "provider_id": provider_id,
        "provider_account_id": provider_account_id,
        "project_id": provider_device.project_id,
        "product_id": provider_device.product_id,
        "home_id": provider_device.home_id,
        "provider_device_id": provider_device.provider_device_id,
        "provider_device_name": provider_device.provider_device_name,
        "native_keys": dict(sorted(provider_device.native_keys.items())),
    }
    encoded = json.dumps(
        stable_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"provider-device.v1:{hashlib.sha256(encoded).hexdigest()}"


class DeviceBindingService:
    def __init__(
        self,
        repository: SleepDomainRepository,
        *,
        access_policy: AdministrativeAccessPolicy,
    ) -> None:
        self.repository = repository
        self.access_policy = access_policy

    def apply(
        self,
        namespace: DomainNamespace,
        command: DeviceBindingCommand,
    ) -> DeviceBinding:
        if command.data_mode != namespace.data_mode:
            raise ValueError("binding command data_mode does not match namespace")
        self.access_policy.require(
            actor_id=command.actor_id,
            authorization_id=command.authorization_id,
            scope=AdministrativeScope.DEVICE_BINDING_WRITE,
        )
        binding = DeviceBinding(
            data_mode=command.data_mode,
            device_binding_id=command.device_binding_id,
            binding_version=command.expected_binding_version + 1,
            device_id=command.device_id,
            provider_id=command.provider_id,
            provider_account_id=command.provider_account_id,
            provider_device=command.provider_device,
            subject_id=command.subject_id,
            timezone_name=command.timezone_name,
            effective_from=command.effective_from,
            effective_until=command.effective_until,
            status=DeviceBindingStatus.ACTIVE,
            changed_by_actor_id=command.actor_id,
            change_reason=command.change_reason,
            recorded_at=command.requested_at,
        )
        history = self.repository.list_device_bindings(
            namespace,
            device_id=command.device_id,
        )
        previous = next(
            (
                item
                for item in history
                if item.binding_version == command.expected_binding_version
            ),
            None,
        )
        if command.expected_binding_version > 0 and previous is None:
            raise CasConflictError(
                "expected DeviceBinding version does not exist"
            )
        action = (
            DeviceBindingAuditAction.CREATED
            if command.expected_binding_version == 0
            else DeviceBindingAuditAction.REBOUND
        )
        provider_device_key = namespaced_provider_device_key(
            provider_id=command.provider_id,
            provider_account_id=command.provider_account_id,
            provider_device=command.provider_device,
        )
        audit = DeviceBindingAuditEvent(
            audit_event_id=f"binding-audit:{command.command_id}",
            command_id=command.command_id,
            data_mode=command.data_mode,
            action=action,
            provider_id=command.provider_id,
            provider_account_id=command.provider_account_id,
            provider_device_key=provider_device_key,
            device_id=command.device_id,
            previous_device_binding_id=(
                None if previous is None else previous.device_binding_id
            ),
            previous_binding_version=(
                None if previous is None else previous.binding_version
            ),
            new_device_binding_id=command.device_binding_id,
            new_binding_version=binding.binding_version,
            effective_at=binding.effective_from,
            actor_id=command.actor_id,
            authorization_id=command.authorization_id,
            reason=command.change_reason,
            occurred_at=command.requested_at,
        )
        return self.repository.compare_and_set_device_binding(
            namespace,
            binding=binding,
            expected_binding_version=command.expected_binding_version,
            provider_device_key=provider_device_key,
            audit_event=audit,
        )


__all__ = [
    "AdministrativeAccessPolicy",
    "AdministrativeAuthorizationError",
    "AdministrativeScope",
    "DeviceBindingCommand",
    "DeviceBindingService",
    "namespaced_provider_device_key",
]
