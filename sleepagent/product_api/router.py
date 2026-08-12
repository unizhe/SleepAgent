"""Synchronous FastAPI transport for the product sleep surface."""

from __future__ import annotations

from typing import Annotated, Callable, cast

from fastapi import APIRouter, Header, Query, Request

from sleepagent.product_api.contracts import (
    AcceptedOperationResponse,
    ErrorResponse,
    FeedbackRequest,
    InteractionAnswerRequest,
    InteractionAskRequest,
    InteractionDecisionRequest,
    InteractionStartRequest,
    InteractionStatusResponse,
    ProductCareResponse,
    ProductRecordsResponse,
    ProductSleepTodayResponse,
    ProductTrendsResponse,
)
from sleepagent.product_api.service import ProductApiService


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

    return router


__all__ = ["ProductServiceProvider", "create_product_router"]
