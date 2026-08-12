"""Replay-only demo controller surface.

The server side never imports verifier oracle files.  Implementations receive
only allowlisted scenario identifiers and enqueue durable operations.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping, Protocol

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DemoModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DemoApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail={
                "code": code,
                "message": message,
                "retryable": retryable,
                "data_mode": "replay",
                "synthetic_non_release": True,
            },
        )
        self.code = code
        self.message = message
        self.retryable = retryable


class DemoErrorResponse(DemoModel):
    schema_version: Literal["demo_error.v1"] = "demo_error.v1"
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    correlation_id: str = Field(min_length=1)


class DemoSeedRequest(DemoModel):
    artifact_family: str = Field(min_length=1, max_length=100)
    scenario_id: str = Field(min_length=1, max_length=200)
    batch_size: int = Field(default=100, ge=1, le=100)


class DemoAdvanceRequest(DemoModel):
    seconds: int = Field(ge=1, le=604_800)


class DemoResetRequest(DemoModel):
    confirmation: Literal["reset-replay-generation"]


class DemoAcceptedResponse(DemoModel):
    schema_version: Literal["demo_accepted.v1"] = "demo_accepted.v1"
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    operation_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    status_url: str = Field(min_length=1)


class DemoOperationResponse(DemoModel):
    schema_version: Literal["demo_operation.v1"] = "demo_operation.v1"
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    operation_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    state: Literal[
        "accepted",
        "staging_input",
        "waiting_normalization",
        "waiting_episode",
        "waiting_fast_path",
        "waiting_product",
        "verifying_views",
        "succeeded",
        "blocked",
        "reconciliation_required",
        "failed",
    ]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "DemoOperationResponse":
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must include a timezone offset")
        if self.state == "succeeded":
            if self.result is None or self.error_code is not None:
                raise ValueError("succeeded operation requires result only")
        elif self.state in {"blocked", "reconciliation_required", "failed"}:
            if self.error_code is None:
                raise ValueError("terminal failure operation requires error_code")
        elif self.result is not None or self.error_code is not None:
            raise ValueError("pending operation cannot expose terminal fields")
        return self


class ScenarioClockResponse(DemoModel):
    schema_version: Literal["scenario_clock.v1"] = "scenario_clock.v1"
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    scenario_time: datetime
    generation: int = Field(ge=1)

    @field_validator("scenario_time")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scenario_time must include a timezone offset")
        return value


class DemoTraceEntry(DemoModel):
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=100)
    root_operation_id: str = Field(min_length=1)
    operation_id: str | None = None
    state: str = Field(min_length=1, max_length=100)
    correlation_id: str | None = None
    night_episode_revision_id: str | None = None
    fast_path_operation_id: str | None = None
    product_operation_id: str | None = None
    analysis_revision_id: str | None = None
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value


class DemoTraceResponse(DemoModel):
    schema_version: Literal["demo_trace.v1"] = "demo_trace.v1"
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    generation: int = Field(ge=1)
    entries: tuple[DemoTraceEntry, ...]
    next_cursor: str | None = None


class DemoController(Protocol):
    def seed(
        self,
        *,
        request: DemoSeedRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse: ...

    def operation(self, *, operation_id: str) -> DemoOperationResponse: ...

    def advance(
        self,
        *,
        request: DemoAdvanceRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse: ...

    def reset(
        self,
        *,
        request: DemoResetRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse: ...

    def clock(self) -> ScenarioClockResponse: ...

    def trace(
        self,
        *,
        operation_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> DemoTraceResponse: ...


def create_demo_router(
    controller: DemoController,
    *,
    token: str,
) -> APIRouter:
    if len(token.encode("utf-8")) < 32:
        raise ValueError("demo-controller token must contain at least 32 bytes")
    router = APIRouter(
        prefix="/demo/v1",
        tags=["Replay demo controller"],
        responses={
            400: {"model": DemoErrorResponse},
            401: {"model": DemoErrorResponse},
            403: {"model": DemoErrorResponse},
            404: {"model": DemoErrorResponse},
            409: {"model": DemoErrorResponse},
            413: {
                "model": DemoErrorResponse,
                "description": "Content Too Large",
            },
            422: {
                "model": DemoErrorResponse,
                "description": "Unprocessable Content",
            },
            501: {"model": DemoErrorResponse},
            503: {"model": DemoErrorResponse},
        },
    )

    def authorize(provided: str | None) -> None:
        if provided is None or not secrets.compare_digest(provided, token):
            raise DemoApiError(
                401,
                "authentication_failed",
                "Demo controller authentication failed.",
            )

    @router.post("/seed", response_model=DemoAcceptedResponse, status_code=202)
    def seed(
        payload: DemoSeedRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> DemoAcceptedResponse:
        authorize(demo_token)
        return controller.seed(
            request=payload,
            idempotency_key=_require_key(idempotency_key),
        )

    @router.post("/advance", response_model=DemoAcceptedResponse, status_code=202)
    def advance(
        payload: DemoAdvanceRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> DemoAcceptedResponse:
        authorize(demo_token)
        return controller.advance(
            request=payload,
            idempotency_key=_require_key(idempotency_key),
        )

    @router.post("/reset", response_model=DemoAcceptedResponse, status_code=202)
    def reset(
        payload: DemoResetRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> DemoAcceptedResponse:
        authorize(demo_token)
        return controller.reset(
            request=payload,
            idempotency_key=_require_key(idempotency_key),
        )

    @router.get(
        "/operations/{operation_id}",
        response_model=DemoOperationResponse,
    )
    def operation(
        operation_id: str,
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> DemoOperationResponse:
        authorize(demo_token)
        return controller.operation(operation_id=operation_id)

    @router.get("/clock", response_model=ScenarioClockResponse)
    def clock(
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> ScenarioClockResponse:
        authorize(demo_token)
        return controller.clock()

    @router.get("/trace", response_model=DemoTraceResponse)
    def trace(
        operation_id: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=2_000)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        demo_token: Annotated[
            str | None,
            Header(alias="X-Demo-Controller-Token"),
        ] = None,
    ) -> DemoTraceResponse:
        authorize(demo_token)
        return controller.trace(
            operation_id=operation_id,
            cursor=cursor,
            limit=limit,
        )

    return router


def _require_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise DemoApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required.",
        )
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 200:
        raise DemoApiError(
            400,
            "invalid_request",
            "Idempotency-Key is too long.",
        )
    return normalized


__all__ = [
    "DemoAcceptedResponse",
    "DemoAdvanceRequest",
    "DemoApiError",
    "DemoController",
    "DemoErrorResponse",
    "DemoOperationResponse",
    "DemoResetRequest",
    "DemoSeedRequest",
    "DemoTraceEntry",
    "DemoTraceResponse",
    "ScenarioClockResponse",
    "create_demo_router",
]
