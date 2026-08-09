"""Capability-scoped transport service adapters for backend composition.

This module contains no lifecycle ownership.  Adapters are constructed before
the pool is opened and acquire database transactions only when invoked.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException

from sleepagent.backend_runtime import RuntimeServices
from sleepagent.backend_settings import ApiSurface, ProcessRole, SleepBackendSettings
from sleepagent.backend_keys import BackendKeyProvider
from sleepagent.backend_persistence import (
    PostgresAuthorityStore,
    PostgresProductBackend,
    PostgresProductIdentityResolver,
    build_product_authenticator,
)
from sleepagent.demo_api import (
    DemoAcceptedResponse,
    DemoAdvanceRequest,
    DemoResetRequest,
    DemoSeedRequest,
    DemoTraceResponse,
    ScenarioClockResponse,
)
from sleepagent.product_api.service import (
    FailClosedProductIdentityResolver,
    ProductApiService,
    ProductBackend,
    ProductRequestContext,
)
from sleepagent.sleep_api.contracts import PublicErrorCode
from sleepagent.sleep_api.auth import SleepApiSecurityError
from sleepagent.sleep_api.postgres_runtime import build_postgres_sleep_api_runtime


class _UnavailableProductBackend(ProductBackend):
    def list_role_projections(
        self,
        context: ProductRequestContext,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[Any, ...], str | None]:
        del context, kind, limit, cursor
        raise RuntimeError("product persistence adapter is unavailable")

    def reserve_command(
        self,
        context: ProductRequestContext,
        *,
        route_template: str,
        command_type: str,
        idempotency_key: str,
        body_sha256: str,
        payload: Mapping[str, Any],
        target_id: str | None,
    ) -> str:
        del (
            context,
            route_template,
            command_type,
            idempotency_key,
            body_sha256,
            payload,
            target_id,
        )
        raise RuntimeError("product persistence adapter is unavailable")

    def get_operation(
        self,
        context: ProductRequestContext,
        *,
        operation_id: str,
    ) -> None:
        del context, operation_id
        raise RuntimeError("product persistence adapter is unavailable")


class _UnavailableSleepAuthenticator:
    @staticmethod
    def _raise() -> None:
        raise SleepApiSecurityError(
            PublicErrorCode.AUTHORIZATION_UNAVAILABLE,
            "The PostgreSQL identity authority is not configured.",
            status_code=503,
            retryable=True,
        )

    def authenticate(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._raise()

    def verify_identity(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._raise()


class _UnavailableSleepRuntime:
    authenticator = _UnavailableSleepAuthenticator()


class _UnavailableDemoController:
    @staticmethod
    def _raise() -> None:
        raise HTTPException(
            status_code=503,
            detail="durable replay controller is unavailable",
        )

    def seed(
        self,
        *,
        request: DemoSeedRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        del request, idempotency_key
        self._raise()

    def advance(
        self,
        *,
        request: DemoAdvanceRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        del request, idempotency_key
        self._raise()

    def reset(
        self,
        *,
        request: DemoResetRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        del request, idempotency_key
        self._raise()

    def clock(self) -> ScenarioClockResponse:
        self._raise()

    def trace(
        self,
        *,
        operation_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> DemoTraceResponse:
        del operation_id, cursor, limit
        self._raise()


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
    demo = (
        _UnavailableDemoController()
        if ApiSurface.DEMO in settings.enabled_surfaces
        else None
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
    )


__all__ = ["build_api_runtime_services"]
