"""Capability-scoped transport service adapters for backend composition.

This module contains no lifecycle ownership.  Adapters are constructed before
the pool is opened and acquire database transactions only when invoked.
"""

from __future__ import annotations

from typing import Any, Mapping

from sleepagent.backend_runtime import RuntimeServices
from sleepagent.backend_settings import ApiSurface, ProcessRole, SleepBackendSettings
from sleepagent.backend_keys import BackendKeyProvider
from sleepagent.backend_persistence import (
    PostgresAuthorityStore,
    PostgresProductBackend,
    PostgresProductIdentityResolver,
    build_product_authenticator,
)
from sleepagent.demo_persistence import DurableDemoController, PostgresDemoStore
from sleepagent.product_api.service import (
    ProductApiService,
)
from sleepagent.persistence.uow import InternalControlScope
from sleepagent.sleep_api.postgres_runtime import build_postgres_sleep_api_runtime


class PostgresInternalStatus:
    """Read non-PHI reconciliation summaries through one protected function."""

    def __init__(self, settings: SleepBackendSettings, uow_factory: object) -> None:
        self.settings = settings
        self.uow_factory = uow_factory

    def reconciliation_status(self, operation_id: str) -> dict[str, Any] | None:
        if not operation_id.strip() or len(operation_id) > 200:
            return None
        scope = InternalControlScope(
            data_mode=self.settings.data_mode.value,
            service_principal_id=self.settings.service_principal_id,
        )
        with self.uow_factory.begin(scope) as uow:  # type: ignore[attr-defined]
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT public.sleepagent_internal_reconciliation_status(%s)",
                    (operation_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, str):
            import json

            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise RuntimeError("internal reconciliation status is not an object")
        return dict(value)

    def operational_metrics(self) -> dict[str, Any]:
        scope = InternalControlScope(
            data_mode=self.settings.data_mode.value,
            service_principal_id=self.settings.service_principal_id,
        )
        with self.uow_factory.begin(scope) as uow:  # type: ignore[attr-defined]
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT public.sleepagent_internal_operational_metrics()"
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None or row[0] is None:
            raise RuntimeError("internal operational metrics are unavailable")
        value = row[0]
        if isinstance(value, str):
            import json

            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise RuntimeError("internal operational metrics are not an object")
        return dict(value)


def build_api_runtime_services(
    settings: SleepBackendSettings,
    *,
    uow_factory: object,
) -> RuntimeServices:
    """Build capability-scoped PostgreSQL adapters for enabled API surfaces."""

    if settings.process_role != ProcessRole.API:
        raise ValueError("API services require an API capability profile")
    if uow_factory is None:
        raise ValueError("API services require a shared UnitOfWorkFactory")
    product = None
    if ApiSurface.PRODUCT in settings.enabled_surfaces:
        key_provider = BackendKeyProvider(settings.deployment_mode)
        authenticator = build_product_authenticator(settings, uow_factory)  # type: ignore[arg-type]
        product = ProductApiService(
            identity_resolver=PostgresProductIdentityResolver(
                authenticator=authenticator,
                authority=PostgresAuthorityStore(
                    settings,
                    uow_factory,  # type: ignore[arg-type]
                ),
            ),
            backend=PostgresProductBackend(
                uow_factory,  # type: ignore[arg-type]
                cursor_key=key_provider.encryption_key(
                    settings.encryption_key_ref
                ),
            ),
        )
    demo = None
    if ApiSurface.DEMO in settings.enabled_surfaces:
        demo = DurableDemoController(
            PostgresDemoStore(
                settings,
                uow_factory,  # type: ignore[arg-type]
            )
        )
    public_provider = None
    if ApiSurface.PUBLIC_V1 in settings.enabled_surfaces:
        key_provider = BackendKeyProvider(settings.deployment_mode)
        public_runtime = build_postgres_sleep_api_runtime(
            settings,
            uow_factory,  # type: ignore[arg-type]
            cursor_key=key_provider.encryption_key(settings.encryption_key_ref),
        )
        public_provider = lambda runtime=public_runtime: runtime
    return RuntimeServices(
        product=product,
        demo=demo,
        public_v1_runtime_provider=public_provider,
        internal_status=(
            PostgresInternalStatus(settings, uow_factory)
            if ApiSurface.INTERNAL in settings.enabled_surfaces
            else None
        ),
    )


__all__ = ["PostgresInternalStatus", "build_api_runtime_services"]
