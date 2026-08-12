"""Application service for product commands and committed role projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, cast

from starlette.requests import Request

from sleepagent.product_api.contracts import (
    AcceptedOperationResponse,
    InteractionStatusResponse,
    ProductCareResponse,
    ProductRecordsResponse,
    ProductRole,
    ProductSleepTodayNoData,
    ProductSleepTodayProjection,
    ProductSleepTodayResponse,
    ProductTrendsResponse,
)


READ_SCOPES = {
    "today": "product:sleep:today:read",
    "trends": "product:sleep:trends:read",
    "care": "product:sleep:care:read",
    "records": "product:sleep:records:read",
}
COMMAND_SCOPES = {
    "interaction.start": "product:sleep:interaction:write",
    "interaction.ask": "product:sleep:interaction:write",
    "interaction.answer": "product:sleep:interaction:answer",
    "interaction.confirm": "product:sleep:care:confirm",
    "interaction.decline": "product:sleep:care:confirm",
    "interaction.feedback": "product:sleep:feedback:write",
}


class ProductApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ProductRequestContext:
    service_principal_id: str
    actor_id: str
    binding_id: str
    subject_id: str
    role: ProductRole
    effective_scopes: frozenset[str]
    namespace_id: str
    namespace_generation: int
    data_mode: str
    run_id: str | None
    arm_id: str | None
    purpose: str
    authorization_epoch: int
    privacy_epoch: int
    retrieval_epoch: int
    policy_sha256: str

    def require_scope(self, scope: str) -> None:
        if scope not in self.effective_scopes:
            raise ProductApiError(
                "authorization_denied",
                "The authoritative binding does not grant this action.",
                status_code=403,
            )


class ProductIdentityResolver(Protocol):
    def resolve(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext: ...


class ProductBackend(Protocol):
    def get_today_projection(
        self,
        context: ProductRequestContext,
    ) -> ProductSleepTodayProjection | None: ...

    def get_trends(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductTrendsResponse: ...

    def get_records(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductRecordsResponse: ...

    def get_care(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductCareResponse: ...

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
    ) -> str: ...

    def get_operation(
        self,
        context: ProductRequestContext,
        *,
        operation_id: str,
    ) -> InteractionStatusResponse | None: ...


class ProductApiService:
    def __init__(
        self,
        *,
        identity_resolver: ProductIdentityResolver,
        backend: ProductBackend,
    ) -> None:
        self.identity_resolver = identity_resolver
        self.backend = backend

    def today(self, request: Request) -> ProductSleepTodayResponse:
        context = self.identity_resolver.resolve(
            request,
            body=b"",
            purpose="sleep_care",
        )
        context.require_scope(READ_SCOPES["today"])
        projection = self.backend.get_today_projection(context)
        if projection is None:
            return ProductSleepTodayNoData(
                data_mode=cast(Literal["live", "replay"], context.data_mode),
                synthetic_non_release=context.data_mode == "replay",
                subject_ref=context.subject_id,
                role=context.role,
            )
        if (
            projection.role != context.role
            or projection.subject_ref != context.subject_id
            or projection.data_mode != context.data_mode
        ):
            raise RuntimeError(
                "backend returned a projection outside the authorized role view"
            )
        return projection

    def query(
        self,
        request: Request,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
    ) -> ProductTrendsResponse | ProductCareResponse | ProductRecordsResponse:
        try:
            required_scope = READ_SCOPES[kind]
        except KeyError as exc:
            raise ProductApiError(
                "invalid_request",
                "Unknown projection kind.",
                status_code=400,
            ) from exc
        context = self.identity_resolver.resolve(
            request,
            body=b"",
            purpose="sleep_care",
        )
        context.require_scope(required_scope)
        if kind == "trends":
            return self.backend.get_trends(context, limit=limit, cursor=cursor)
        if kind == "records":
            return self.backend.get_records(context, limit=limit, cursor=cursor)
        if kind == "care":
            return self.backend.get_care(context, limit=limit, cursor=cursor)
        raise RuntimeError("Product read dispatch drifted")

    def submit(
        self,
        request: Request,
        *,
        route_template: str,
        command_type: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
        target_id: str | None = None,
        request_body: bytes | None = None,
    ) -> AcceptedOperationResponse:
        if command_type not in COMMAND_SCOPES:
            raise ProductApiError(
                "invalid_request",
                "Unknown command type.",
                status_code=400,
            )
        if request_body is None:
            raise ProductApiError(
                "authenticated_body_missing",
                "The authenticated request body is required.",
                status_code=400,
            )
        body = request_body
        context = self.identity_resolver.resolve(
            request,
            body=body,
            purpose="sleep_care",
        )
        context.require_scope(COMMAND_SCOPES[command_type])
        if command_type in {"interaction.confirm", "interaction.decline"}:
            if context.role == ProductRole.DOCTOR:
                raise ProductApiError(
                    "authorization_denied",
                    "Doctor role cannot confirm a personal Care action.",
                    status_code=403,
                )
        if command_type == "interaction.feedback" and context.role == ProductRole.DOCTOR:
            raise ProductApiError(
                "authorization_denied",
                "Doctor role cannot author elder or family feedback facts.",
                status_code=403,
            )
        caller_key = _idempotency_key(idempotency_key)
        operation_id = self.backend.reserve_command(
            context,
            route_template=route_template,
            command_type=command_type,
            idempotency_key=caller_key,
            body_sha256=_sha256_bytes(body),
            payload=payload,
            target_id=target_id,
        )
        return AcceptedOperationResponse(
            data_mode=cast(Literal["live", "replay"], context.data_mode),
            synthetic_non_release=context.data_mode == "replay",
            operation_id=operation_id,
            status_url=(
                "/product/sleep/interactions/status/" + operation_id
            ),
        )

    def status(
        self,
        request: Request,
        *,
        operation_id: str,
    ) -> InteractionStatusResponse:
        context = self.identity_resolver.resolve(
            request,
            body=b"",
            purpose="sleep_care",
        )
        context.require_scope("product:sleep:operation:read")
        result = self.backend.get_operation(
            context,
            operation_id=operation_id,
        )
        if result is None:
            raise ProductApiError(
                "not_found",
                "Operation was not found.",
                status_code=404,
            )
        if result.data_mode != context.data_mode:
            raise RuntimeError("backend returned a cross-mode Operation")
        return result


class FailClosedProductIdentityResolver:
    def resolve(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext:
        del request, body, purpose
        raise ProductApiError(
            "authorization_unavailable",
            "Product identity authority is not configured.",
            status_code=503,
            retryable=True,
        )


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise ProductApiError(
            "idempotency_key_required",
            "Idempotency-Key is required.",
            status_code=400,
        )
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 200:
        raise ProductApiError(
            "invalid_idempotency_key",
            "Idempotency-Key is too long.",
            status_code=400,
        )
    return normalized


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "COMMAND_SCOPES",
    "FailClosedProductIdentityResolver",
    "ProductApiError",
    "ProductApiService",
    "ProductBackend",
    "ProductIdentityResolver",
    "ProductRequestContext",
    "READ_SCOPES",
]
