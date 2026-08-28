# 本模块负责唯一 ASGI 服务的接口契约或请求编排，不承载领域状态。
"""Synchronous FastAPI transport for the product sleep surface."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Callable, cast

from fastapi import APIRouter, Header, Query, Request

from sleepagent.api.product_contracts import (
    AcceptedOperationResponse,
    ErrorResponse,
    FeedbackRequest,
    HabitChangeRequest,
    HabitChangeResponse,
    HabitProfileResponse,
    HabitQuestionSelectionRequest,
    HabitQuestionSelectionResponse,
    InteractionAnswerRequest,
    InteractionAskRequest,
    InteractionDecisionRequest,
    InteractionStartRequest,
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
    ProductSleepReportListResponse,
    ProductSleepReportResponse,
    ProductSleepTodayResponse,
    ProductTrendsResponse,
)
from sleepagent.api.product import ProductApiService


ProductServiceProvider = Callable[[], ProductApiService]


def create_product_router(provider: ProductServiceProvider) -> APIRouter:
    router = APIRouter(
        prefix="/product/sleep",
        tags=["Product sleep"],
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {
                "model": ErrorResponse,
                "description": "Content Too Large",
            },
            422: {
                "model": ErrorResponse,
                "description": "Unprocessable Content",
            },
            501: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )

    @router.get("/today", response_model=ProductSleepTodayResponse)
    def today(
        request: Request,
    ) -> ProductSleepTodayResponse:
        return provider().today(request)

    @router.get("/trends", response_model=ProductTrendsResponse)
    def trends(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=90)] = 30,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProductTrendsResponse:
        return cast(ProductTrendsResponse, provider().query(
            request,
            kind="trends",
            limit=limit,
            cursor=cursor,
        ))

    @router.get("/care", response_model=ProductCareResponse)
    def care(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProductCareResponse:
        return cast(ProductCareResponse, provider().query(
            request,
            kind="care",
            limit=limit,
            cursor=cursor,
        ))

    @router.get("/records", response_model=ProductRecordsResponse)
    def records(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProductRecordsResponse:
        return cast(ProductRecordsResponse, provider().query(
            request,
            kind="records",
            limit=limit,
            cursor=cursor,
        ))

    @router.post(
        "/reports/run",
        response_model=ProductReportRunAccepted,
        status_code=202,
    )
    async def run_report(
        payload: ProductReportRunRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ProductReportRunAccepted:
        return provider().run_report(
            request,
            payload,
            idempotency_key=idempotency_key,
            request_body=await request.body(),
        )

    @router.get(
        "/reports",
        response_model=ProductSleepReportListResponse,
    )
    async def list_reports(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
        trace: bool = False,
    ) -> ProductSleepReportListResponse:
        return provider().list_reports(
            request,
            limit=limit,
            cursor=cursor,
            trace=trace,
            request_body=await request.body(),
        )

    @router.get(
        "/reports/{wake_date}",
        response_model=ProductSleepReportResponse,
    )
    async def show_report(
        wake_date: date,
        request: Request,
        trace: bool = False,
    ) -> ProductSleepReportResponse:
        return provider().show_report(
            request,
            wake_date=wake_date,
            trace=trace,
            request_body=await request.body(),
        )

    @router.post(
        "/interactions/start",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def start(
        payload: InteractionStartRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=payload.episode_revision_id,
            request_body=await request.body(),
        )

    @router.post(
        "/interactions/{interaction_id}/ask",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def ask(
        interaction_id: str,
        payload: InteractionAskRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/{interaction_id}/ask",
            command_type="interaction.ask",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=interaction_id,
            request_body=await request.body(),
        )

    @router.get(
        "/interactions/status/{operation_id}",
        response_model=InteractionStatusResponse,
    )
    def status(
        operation_id: str,
        request: Request,
    ) -> InteractionStatusResponse:
        return provider().status(request, operation_id=operation_id)

    @router.post(
        "/interactions/{interaction_id}/answer",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def answer(
        interaction_id: str,
        payload: InteractionAnswerRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template=(
                "/product/sleep/interactions/{interaction_id}/answer"
            ),
            command_type="interaction.answer",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=interaction_id,
            request_body=await request.body(),
        )

    @router.post(
        "/interactions/{interaction_id}/confirm",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def confirm(
        interaction_id: str,
        payload: InteractionDecisionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template=(
                "/product/sleep/interactions/{interaction_id}/confirm"
            ),
            command_type="interaction.confirm",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=interaction_id,
            request_body=await request.body(),
        )

    @router.post(
        "/interactions/{interaction_id}/decline",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def decline(
        interaction_id: str,
        payload: InteractionDecisionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template=(
                "/product/sleep/interactions/{interaction_id}/decline"
            ),
            command_type="interaction.decline",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=interaction_id,
            request_body=await request.body(),
        )

    @router.post(
        "/interactions/feedback",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    async def feedback(
        payload: FeedbackRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/feedback",
            command_type="interaction.feedback",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=payload.interaction_id or payload.episode_revision_id,
            request_body=await request.body(),
        )

    @router.post(
        "/personalization/habit/questions",
        response_model=HabitQuestionSelectionResponse,
    )
    async def habit_questions(
        payload: HabitQuestionSelectionRequest,
        request: Request,
    ) -> HabitQuestionSelectionResponse:
        return provider().habit_questions(
            request,
            payload,
            request_body=await request.body(),
        )

    @router.post(
        "/personalization/habit/changes",
        response_model=HabitChangeResponse,
    )
    async def habit_change(
        payload: HabitChangeRequest,
        request: Request,
    ) -> HabitChangeResponse:
        return provider().habit_change(
            request,
            payload,
            request_body=await request.body(),
        )

    @router.post(
        "/personalization/habit/confirm",
        response_model=L2ConfirmationResponse,
    )
    async def habit_confirm(
        payload: L2ConfirmationRequest,
        request: Request,
    ) -> L2ConfirmationResponse:
        return provider().confirm_personalization(
            request,
            payload,
            capability="habit",
            request_body=await request.body(),
        )

    @router.get(
        "/personalization/habit",
        response_model=HabitProfileResponse,
    )
    def habit_profile(request: Request) -> HabitProfileResponse:
        return provider().habit_profile(request)

    @router.post(
        "/personalization/memory/changes",
        response_model=PendingL2Change,
    )
    async def memory_change(
        payload: MemoryChangeRequest,
        request: Request,
    ) -> PendingL2Change:
        return provider().memory_change(
            request,
            payload,
            request_body=await request.body(),
        )

    @router.post(
        "/personalization/memory/confirm",
        response_model=L2ConfirmationResponse,
    )
    async def memory_confirm(
        payload: L2ConfirmationRequest,
        request: Request,
    ) -> L2ConfirmationResponse:
        return provider().confirm_personalization(
            request,
            payload,
            capability="memory",
            request_body=await request.body(),
        )

    @router.post(
        "/personalization/memory/query",
        response_model=MemoryQueryResponse,
    )
    async def memory_query(
        payload: MemoryQueryRequest,
        request: Request,
    ) -> MemoryQueryResponse:
        return provider().memory_query(
            request,
            payload,
            request_body=await request.body(),
        )

    return router


__all__ = ["ProductServiceProvider", "create_product_router"]
