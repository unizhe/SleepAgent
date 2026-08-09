"""Replay-only demo controller surface.

The server side never imports verifier oracle files.  Implementations receive
only allowlisted scenario identifiers and enqueue durable operations.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Literal, Mapping, Protocol

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DemoModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DemoSeedRequest(DemoModel):
    artifact_family: str = Field(min_length=1, max_length=100)
    scenario_id: str = Field(min_length=1, max_length=200)
    batch_size: int = Field(default=250, ge=1, le=1_000)


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
    operation_id: str | None = None
    state: str = Field(min_length=1, max_length=100)
    correlation_id: str | None = None
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
    router = APIRouter(prefix="/demo/v1", tags=["Replay demo controller"])

    def authorize(provided: str | None) -> None:
        if provided is None or not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="demo controller required")

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
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return normalized


__all__ = [
    "DemoAcceptedResponse",
    "DemoAdvanceRequest",
    "DemoController",
    "DemoResetRequest",
    "DemoSeedRequest",
    "DemoTraceEntry",
    "DemoTraceResponse",
    "ScenarioClockResponse",
    "create_demo_router",
]
