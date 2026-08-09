"""Application service for product commands and committed role projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from starlette.requests import Request

from sleepagent.product_api.contracts import (
    AcceptedOperationResponse,
    InteractionStatusResponse,
    ProductRole,
    ProjectionRecord,
    ProjectionResponse,
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
    def list_role_projections(
        self,
        context: ProductRequestContext,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[ProjectionRecord, ...], str | None]: ...

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

    def query(
        self,
        request: Request,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
    ) -> ProjectionResponse:
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
        items, next_cursor = self.backend.list_role_projections(
            context,
            kind=kind,
            limit=limit,
            cursor=cursor,
        )
        if any(
            item.role != context.role or item.subject_ref != context.subject_id
            for item in items
        ):
            raise RuntimeError(
                "backend returned a projection outside the authorized role view"
            )
        return ProjectionResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            kind=kind,
            items=items,
            next_cursor=next_cursor,
        )

    def submit(
        self,
        request: Request,
        *,
        route_template: str,
        command_type: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
        target_id: str | None = None,
    ) -> AcceptedOperationResponse:
        if command_type not in COMMAND_SCOPES:
            raise ProductApiError(
                "invalid_request",
                "Unknown command type.",
                status_code=400,
            )
        key = _idempotency_key(idempotency_key)
        body = _canonical_json(payload)
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
        operation_id = self.backend.reserve_command(
            context,
            route_template=route_template,
            command_type=command_type,
            idempotency_key=key,
            body_sha256=hashlib.sha256(body).hexdigest(),
            payload=payload,
            target_id=target_id,
        )
        return AcceptedOperationResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            operation_id=operation_id,
            status_url=f"/product/sleep/interactions/status/{operation_id}",
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


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda value: value.isoformat(),
    ).encode("utf-8")


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
