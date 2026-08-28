# 本模块负责唯一 ASGI 服务的接口契约或请求编排，不承载领域状态。
"""Application service for product commands and committed role projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping, Protocol, cast

from starlette.requests import Request

from sleepagent.api.product_contracts import (
    AcceptedOperationResponse,
    HabitChangeRequest,
    HabitChangeResponse,
    HabitProfileResponse,
    HabitQuestionSelectionRequest,
    HabitQuestionSelectionResponse,
    InteractionStatusResponse,
    L2ConfirmationRequest,
    L2ConfirmationResponse,
    MemoryChangeRequest,
    MemoryQueryRequest,
    MemoryQueryResponse,
    PendingL2Change,
    ProductCareResponse,
    ProductReportRunAccepted,
    ProductReportRunRequest,
    ProductRecordsResponse,
    ProductRole,
    ProductSleepReportListResponse,
    ProductSleepReportResponse,
    ProductSleepTodayNoData,
    ProductSleepTodayProjection,
    ProductSleepTodayResponse,
    ProductTrendsResponse,
)
from sleepagent.domain.product_data import public_product_subject_ref


READ_SCOPES = {
    "today": "product:sleep:today:read",
    "trends": "product:sleep:trends:read",
    "care": "product:sleep:care:read",
    "records": "product:sleep:records:read",
    "reports": "product:sleep:today:read",
}
COMMAND_SCOPES = {
    "interaction.start": "product:sleep:interaction:write",
    "interaction.ask": "product:sleep:interaction:write",
    "interaction.answer": "product:sleep:interaction:answer",
    "interaction.confirm": "product:sleep:care:confirm",
    "interaction.decline": "product:sleep:care:confirm",
    "interaction.feedback": "product:sleep:feedback:write",
    "product.report.run.v1": "sleep:reanalysis:write",
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

    def resolve_read(
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

    def reserve_report_run(
        self,
        context: ProductRequestContext,
        *,
        wake_date: date,
        idempotency_key: str,
        body_sha256: str,
    ) -> str: ...

    def get_report(
        self,
        context: ProductRequestContext,
        *,
        wake_date: date,
        trace: bool,
    ) -> ProductSleepReportResponse | None: ...

    def list_reports(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
        trace: bool,
    ) -> ProductSleepReportListResponse: ...

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

    def select_habit_questions(
        self,
        context: ProductRequestContext,
        request: HabitQuestionSelectionRequest,
    ) -> HabitQuestionSelectionResponse: ...

    def propose_habit_changes(
        self,
        context: ProductRequestContext,
        request: HabitChangeRequest,
    ) -> HabitChangeResponse: ...

    def confirm_l2_change(
        self,
        context: ProductRequestContext,
        request: L2ConfirmationRequest,
        *,
        capability: Literal["habit", "memory"],
    ) -> L2ConfirmationResponse: ...

    def get_habit_profile(
        self,
        context: ProductRequestContext,
    ) -> HabitProfileResponse: ...

    def propose_memory_change(
        self,
        context: ProductRequestContext,
        request: MemoryChangeRequest,
    ) -> PendingL2Change: ...

    def query_memory(
        self,
        context: ProductRequestContext,
        request: MemoryQueryRequest,
    ) -> MemoryQueryResponse: ...


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
                subject_ref=public_product_subject_ref(context.subject_id),
                role=context.role,
            )
        if (
            projection.role != context.role
            or projection.subject_ref
            != public_product_subject_ref(context.subject_id)
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

    def run_report(
        self,
        request: Request,
        payload: ProductReportRunRequest,
        *,
        idempotency_key: str | None,
        request_body: bytes | None,
    ) -> ProductReportRunAccepted:
        if request_body is None:
            raise ProductApiError(
                "authenticated_body_missing",
                "The authenticated request body is required.",
                status_code=400,
            )
        context = self.identity_resolver.resolve(
            request,
            body=request_body,
            purpose="sleep_care",
        )
        context.require_scope(COMMAND_SCOPES["product.report.run.v1"])
        caller_key = _idempotency_key(idempotency_key)
        self.backend.reserve_report_run(
            context,
            wake_date=payload.wake_date,
            idempotency_key=caller_key,
            body_sha256=_sha256_bytes(request_body),
        )
        return ProductReportRunAccepted(
            wake_date=payload.wake_date,
            status_url=(
                "/product/sleep/reports/" + payload.wake_date.isoformat()
            ),
        )

    def show_report(
        self,
        request: Request,
        *,
        wake_date: date,
        trace: bool,
        request_body: bytes,
    ) -> ProductSleepReportResponse:
        context = self.identity_resolver.resolve_read(
            request,
            body=request_body,
            purpose="sleep_care",
        )
        context.require_scope(READ_SCOPES["reports"])
        result = self.backend.get_report(
            context,
            wake_date=wake_date,
            trace=trace,
        )
        if result is None:
            raise ProductApiError(
                "not_found",
                "A finalized report night was not found for that wake date.",
                status_code=404,
            )
        if result.wake_date != wake_date or result.audience != context.role:
            raise RuntimeError("backend returned a report outside its authority")
        return result

    def list_reports(
        self,
        request: Request,
        *,
        limit: int,
        cursor: str | None,
        trace: bool,
        request_body: bytes,
    ) -> ProductSleepReportListResponse:
        context = self.identity_resolver.resolve_read(
            request,
            body=request_body,
            purpose="sleep_care",
        )
        context.require_scope(READ_SCOPES["reports"])
        result = self.backend.list_reports(
            context,
            limit=limit,
            cursor=cursor,
            trace=trace,
        )
        dates = tuple(item.wake_date for item in result.items)
        if (
            any(item.audience != context.role for item in result.items)
            or len(dates) != len(set(dates))
            or dates != tuple(sorted(dates, reverse=True))
        ):
            raise RuntimeError("backend returned reports outside their authority")
        return result

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

    def habit_questions(
        self,
        request: Request,
        payload: HabitQuestionSelectionRequest,
        *,
        request_body: bytes,
    ) -> HabitQuestionSelectionResponse:
        context = self._personalization_context(request, request_body)
        context.require_scope("product:sleep:interaction:write")
        if context.role == ProductRole.DOCTOR:
            raise ProductApiError(
                "authorization_denied",
                "Doctor role cannot answer personal Habit questions.",
                status_code=403,
            )
        return self.backend.select_habit_questions(context, payload)

    def habit_change(
        self,
        request: Request,
        payload: HabitChangeRequest,
        *,
        request_body: bytes,
    ) -> HabitChangeResponse:
        context = self._personalization_context(request, request_body)
        context.require_scope("product:sleep:interaction:write")
        if context.role == ProductRole.DOCTOR:
            raise ProductApiError(
                "authorization_denied",
                "Doctor role cannot mutate a personal Habit profile.",
                status_code=403,
            )
        return self.backend.propose_habit_changes(context, payload)

    def habit_profile(self, request: Request) -> HabitProfileResponse:
        context = self._personalization_context(request, b"")
        context.require_scope("product:sleep:today:read")
        return self.backend.get_habit_profile(context)

    def memory_change(
        self,
        request: Request,
        payload: MemoryChangeRequest,
        *,
        request_body: bytes,
    ) -> PendingL2Change:
        context = self._personalization_context(request, request_body)
        context.require_scope("product:sleep:interaction:write")
        if context.role != ProductRole.ELDER:
            raise ProductApiError(
                "authorization_denied",
                "Only the elder may propose governed Memory changes.",
                status_code=403,
            )
        return self.backend.propose_memory_change(context, payload)

    def confirm_personalization(
        self,
        request: Request,
        payload: L2ConfirmationRequest,
        *,
        capability: Literal["habit", "memory"],
        request_body: bytes,
    ) -> L2ConfirmationResponse:
        context = self._personalization_context(request, request_body)
        context.require_scope("product:sleep:care:confirm")
        if context.role != ProductRole.ELDER:
            raise ProductApiError(
                "authorization_denied",
                "Only the elder may confirm an L2 change.",
                status_code=403,
            )
        return self.backend.confirm_l2_change(
            context,
            payload,
            capability=capability,
        )

    def memory_query(
        self,
        request: Request,
        payload: MemoryQueryRequest,
        *,
        request_body: bytes,
    ) -> MemoryQueryResponse:
        context = self._personalization_context(request, request_body)
        context.require_scope("product:sleep:today:read")
        if context.role != ProductRole.ELDER:
            raise ProductApiError(
                "authorization_denied",
                "Explicit governed Memory review belongs to the elder.",
                status_code=403,
            )
        return self.backend.query_memory(context, payload)

    def _personalization_context(
        self,
        request: Request,
        body: bytes,
    ) -> ProductRequestContext:
        return self.identity_resolver.resolve(
            request,
            body=body,
            purpose="sleep_care",
        )


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

    def resolve_read(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext:
        return self.resolve(request, body=body, purpose=purpose)


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
