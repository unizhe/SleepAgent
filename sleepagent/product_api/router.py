"""Synchronous FastAPI transport for the product sleep surface."""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import APIRouter, Header, Query, Request

from sleepagent.product_api.contracts import (
    AcceptedOperationResponse,
    FeedbackRequest,
    InteractionAnswerRequest,
    InteractionAskRequest,
    InteractionDecisionRequest,
    InteractionStartRequest,
    InteractionStatusResponse,
    ProjectionResponse,
)
from sleepagent.product_api.service import ProductApiService


ProductServiceProvider = Callable[[], ProductApiService]


def create_product_router(provider: ProductServiceProvider) -> APIRouter:
    router = APIRouter(prefix="/product/sleep", tags=["Product sleep"])

    @router.get("/today", response_model=ProjectionResponse)
    def today(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=31)] = 1,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProjectionResponse:
        return provider().query(
            request,
            kind="today",
            limit=limit,
            cursor=cursor,
        )

    @router.get("/trends", response_model=ProjectionResponse)
    def trends(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=90)] = 30,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProjectionResponse:
        return provider().query(
            request,
            kind="trends",
            limit=limit,
            cursor=cursor,
        )

    @router.get("/care", response_model=ProjectionResponse)
    def care(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProjectionResponse:
        return provider().query(
            request,
            kind="care",
            limit=limit,
            cursor=cursor,
        )

    @router.get("/records", response_model=ProjectionResponse)
    def records(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
    ) -> ProjectionResponse:
        return provider().query(
            request,
            kind="records",
            limit=limit,
            cursor=cursor,
        )

    @router.post(
        "/interactions/start",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    def start(
        payload: InteractionStartRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=payload.episode_revision_id,
        )

    @router.post(
        "/interactions/{interaction_id}/ask",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    def ask(
        interaction_id: str,
        payload: InteractionAskRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/{interaction_id}/ask",
            command_type="interaction.ask",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=interaction_id,
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
    def answer(
        interaction_id: str,
        payload: InteractionAnswerRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
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
        )

    @router.post(
        "/interactions/{interaction_id}/confirm",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    def confirm(
        interaction_id: str,
        payload: InteractionDecisionRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
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
        )

    @router.post(
        "/interactions/{interaction_id}/decline",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    def decline(
        interaction_id: str,
        payload: InteractionDecisionRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
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
        )

    @router.post(
        "/interactions/feedback",
        response_model=AcceptedOperationResponse,
        status_code=202,
    )
    def feedback(
        payload: FeedbackRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> AcceptedOperationResponse:
        return provider().submit(
            request,
            route_template="/product/sleep/interactions/feedback",
            command_type="interaction.feedback",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            target_id=payload.interaction_id or payload.episode_revision_id,
        )

    return router


__all__ = ["ProductServiceProvider", "create_product_router"]
