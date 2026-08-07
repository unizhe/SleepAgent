"""FastAPI transport for the standalone versioned sleep-domain boundary."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    Query,
    Request,
    Response,
    Security,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from fastapi.exception_handlers import request_validation_exception_handler

from sleepagent.sleep_api.auth import (
    AuthenticatedActorContext,
    SleepApiSecurityError,
)
from sleepagent.sleep_api.compatibility import API_PREFIX, apply_version_headers
from sleepagent.sleep_api.contracts import (
    AcceptedOperationResponse,
    ActivateMonitoringRequest,
    CurrentRiskResponse,
    DeactivateMonitoringRequest,
    ErrorResponse,
    EventPollResponse,
    FeedbackRequest,
    LifecycleResponse,
    NightEpisodePageResponse,
    NightEpisodeResponse,
    OperationStatusResponse,
    PublicActorRole,
    PublicErrorCode,
    ReanalysisRequest,
    RoleViewResponse,
)
from sleepagent.sleep_api.service import (
    SleepApiApplicationError,
    SleepApiRuntime,
)


RuntimeProvider = Callable[[], SleepApiRuntime]
SERVICE_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="SleepServiceCredential",
    description="Rotating HTTPS Bearer credential for the calling service.",
)
ACTOR_ASSERTION = APIKeyHeader(
    name="X-Sleep-Actor-Assertion",
    auto_error=False,
    scheme_name="SleepActorAssertion",
    description="Asymmetric JWS assertion bound to this HTTP request.",
)
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def create_sleep_api_router(runtime_provider: RuntimeProvider) -> APIRouter:
    router = APIRouter(
        prefix=API_PREFIX,
        tags=["Sleep domain v1"],
        responses=ERROR_RESPONSES,
    )

    @router.get(
        "/subjects/{subject_id}/events",
        response_model=EventPollResponse,
        operation_id="pollSleepDomainEventsV1",
    )
    async def poll_events(
        subject_id: str,
        request: Request,
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=3000),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> EventPollResponse:
        runtime = runtime_provider()
        body = await request.body()
        identity = runtime.authenticator.verify_identity(
            request,
            body=body,
            required_scopes=frozenset({"sleep:events:read"}),
        )
        apply_version_headers(response)
        return runtime.poll_events(
            identity,
            subject_id=subject_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/subjects/{subject_id}/lifecycle",
        response_model=LifecycleResponse,
        operation_id="getSleepLifecycleV1",
    )
    async def get_lifecycle(
        subject_id: str,
        request: Request,
        response: Response,
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> LifecycleResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes={"sleep:lifecycle:read"},
        )
        apply_version_headers(response)
        return runtime.get_lifecycle(context, subject_id=subject_id)

    @router.get(
        "/subjects/{subject_id}/risk",
        response_model=CurrentRiskResponse,
        operation_id="getCurrentSleepRiskV1",
    )
    async def get_risk(
        subject_id: str,
        request: Request,
        response: Response,
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> CurrentRiskResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes={"sleep:risk:read"},
        )
        apply_version_headers(response)
        return runtime.get_current_risk(context, subject_id=subject_id)

    @router.get(
        "/subjects/{subject_id}/night-episodes",
        response_model=NightEpisodePageResponse,
        operation_id="listNightEpisodesV1",
    )
    async def list_night_episodes(
        subject_id: str,
        request: Request,
        response: Response,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=3000),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> NightEpisodePageResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes={"sleep:episode:read"},
        )
        apply_version_headers(response)
        return runtime.list_night_episodes(
            context,
            subject_id=subject_id,
            limit=limit,
            cursor=cursor,
        )

    @router.get(
        "/subjects/{subject_id}/night-episodes/{night_episode_id}",
        response_model=NightEpisodeResponse,
        operation_id="getNightEpisodeV1",
    )
    async def get_night_episode(
        subject_id: str,
        night_episode_id: str,
        request: Request,
        response: Response,
        revision_id: str | None = Query(default=None, min_length=1, max_length=200),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> NightEpisodeResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes={"sleep:episode:read"},
        )
        apply_version_headers(response)
        return runtime.get_night_episode(
            context,
            subject_id=subject_id,
            night_episode_id=night_episode_id,
            revision_id=revision_id,
        )

    @router.get(
        "/subjects/{subject_id}/night-episodes/{night_episode_id}/view",
        response_model=RoleViewResponse,
        operation_id="getAuthorizedSleepViewV1",
    )
    async def get_role_view(
        subject_id: str,
        night_episode_id: str,
        request: Request,
        response: Response,
        revision_id: str | None = Query(default=None, min_length=1, max_length=200),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> RoleViewResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes=set(),
        )
        required_scope = {
            PublicActorRole.ELDER: "sleep:view:elder",
            PublicActorRole.FAMILY: "sleep:view:family",
            PublicActorRole.CAREGIVER: "sleep:view:family",
            PublicActorRole.DOCTOR: "sleep:view:doctor",
        }[context.claims.role]
        _require_context_scope(context, required_scope)
        apply_version_headers(response)
        return runtime.get_role_view(
            context,
            subject_id=subject_id,
            night_episode_id=night_episode_id,
            revision_id=revision_id,
        )

    @router.get(
        "/operations/{operation_id}",
        response_model=OperationStatusResponse,
        operation_id="getSleepOperationV1",
    )
    async def get_operation(
        operation_id: str,
        request: Request,
        response: Response,
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> OperationStatusResponse:
        runtime, context = await _authenticate(
            runtime_provider,
            request,
            scopes={"sleep:operation:read"},
        )
        apply_version_headers(response)
        return runtime.get_operation(context, operation_id=operation_id)

    @router.post(
        "/subjects/{subject_id}/monitoring/activate",
        response_model=AcceptedOperationResponse,
        status_code=202,
        operation_id="activateSleepMonitoringV1",
    )
    async def activate_monitoring(
        subject_id: str,
        payload: ActivateMonitoringRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> AcceptedOperationResponse:
        runtime, context, _body = await _command_auth(
            runtime_provider,
            request,
            scopes={"sleep:monitoring:write"},
            roles={
                PublicActorRole.ELDER,
                PublicActorRole.FAMILY,
                PublicActorRole.CAREGIVER,
            },
        )
        runtime.require_subject(context, subject_id)
        apply_version_headers(response)
        return runtime.submit_command(
            context,
            operation_type="sleep_api.monitoring.activate.v1",
            route_template="/api/v1/subjects/{subject_id}/monitoring/activate",
            target_resource_id=subject_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_payload=payload.model_dump(mode="json"),
        )

    @router.post(
        "/subjects/{subject_id}/monitoring/deactivate",
        response_model=AcceptedOperationResponse,
        status_code=202,
        operation_id="deactivateSleepMonitoringV1",
    )
    async def deactivate_monitoring(
        subject_id: str,
        payload: DeactivateMonitoringRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> AcceptedOperationResponse:
        runtime, context, _body = await _command_auth(
            runtime_provider,
            request,
            scopes={"sleep:monitoring:write"},
            roles={
                PublicActorRole.ELDER,
                PublicActorRole.FAMILY,
                PublicActorRole.CAREGIVER,
            },
        )
        runtime.require_subject(context, subject_id)
        apply_version_headers(response)
        return runtime.submit_command(
            context,
            operation_type="sleep_api.monitoring.deactivate.v1",
            route_template="/api/v1/subjects/{subject_id}/monitoring/deactivate",
            target_resource_id=subject_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_payload=payload.model_dump(mode="json"),
        )

    @router.post(
        "/subjects/{subject_id}/feedback/elder",
        response_model=AcceptedOperationResponse,
        status_code=202,
        operation_id="submitElderSleepFeedbackV1",
    )
    async def submit_elder_feedback(
        subject_id: str,
        payload: FeedbackRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> AcceptedOperationResponse:
        runtime, context, _body = await _command_auth(
            runtime_provider,
            request,
            scopes={"sleep:feedback:self"},
            roles={PublicActorRole.ELDER},
        )
        runtime.require_subject(context, subject_id)
        apply_version_headers(response)
        return runtime.submit_command(
            context,
            operation_type="sleep_api.feedback.elder.v1",
            route_template="/api/v1/subjects/{subject_id}/feedback/elder",
            target_resource_id=payload.night_episode_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_payload=payload.model_dump(mode="json"),
        )

    @router.post(
        "/subjects/{subject_id}/feedback/family",
        response_model=AcceptedOperationResponse,
        status_code=202,
        operation_id="submitFamilyCaregiverSleepFeedbackV1",
    )
    async def submit_family_feedback(
        subject_id: str,
        payload: FeedbackRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> AcceptedOperationResponse:
        runtime, context, _body = await _command_auth(
            runtime_provider,
            request,
            scopes={"sleep:feedback:family"},
            roles={PublicActorRole.FAMILY, PublicActorRole.CAREGIVER},
        )
        runtime.require_subject(context, subject_id)
        apply_version_headers(response)
        return runtime.submit_command(
            context,
            operation_type="sleep_api.feedback.family.v1",
            route_template="/api/v1/subjects/{subject_id}/feedback/family",
            target_resource_id=payload.night_episode_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_payload=payload.model_dump(mode="json"),
        )

    @router.post(
        "/subjects/{subject_id}/night-episodes/{night_episode_id}/reanalysis",
        response_model=AcceptedOperationResponse,
        status_code=202,
        operation_id="requestNightEpisodeReanalysisV1",
    )
    async def request_reanalysis(
        subject_id: str,
        night_episode_id: str,
        payload: ReanalysisRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        _service: HTTPAuthorizationCredentials | None = Security(SERVICE_BEARER),
        _actor: str | None = Security(ACTOR_ASSERTION),
    ) -> AcceptedOperationResponse:
        runtime, context, _body = await _command_auth(
            runtime_provider,
            request,
            scopes={"sleep:reanalysis:write"},
            roles=set(PublicActorRole),
        )
        runtime.require_subject(context, subject_id)
        apply_version_headers(response)
        return runtime.submit_command(
            context,
            operation_type="sleep_api.reanalysis.v1",
            route_template=(
                "/api/v1/subjects/{subject_id}/night-episodes/"
                "{night_episode_id}/reanalysis"
            ),
            target_resource_id=night_episode_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_payload=payload.model_dump(mode="json"),
        )

    return router


def install_sleep_api(
    app: FastAPI,
    runtime_provider: RuntimeProvider,
) -> None:
    app.include_router(create_sleep_api_router(runtime_provider))
    app.add_exception_handler(SleepApiSecurityError, sleep_api_error_handler)
    app.add_exception_handler(SleepApiApplicationError, sleep_api_error_handler)
    app.add_exception_handler(RequestValidationError, sleep_api_validation_handler)


async def sleep_api_error_handler(
    request: Request,
    exc: SleepApiSecurityError | SleepApiApplicationError,
) -> JSONResponse:
    correlation_id = (
        request.headers.get("x-correlation-id", "").strip() or "unavailable"
    )[:128]
    response = ErrorResponse(
        code=exc.code,
        message=str(exc),
        correlation_id=correlation_id,
        retryable=exc.retryable,
        details=getattr(exc, "details", {}),
        tombstone=getattr(exc, "tombstone", None),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(mode="json"),
        headers={"X-API-Version": "v1", "Deprecation": "false"},
    )


async def sleep_api_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> Any:
    if not request.url.path.startswith(API_PREFIX + "/"):
        return await request_validation_exception_handler(request, exc)
    correlation_id = (
        request.headers.get("x-correlation-id", "").strip() or "unavailable"
    )[:128]
    response = ErrorResponse(
        code=PublicErrorCode.INVALID_REQUEST,
        message="The request does not match the versioned API contract.",
        correlation_id=correlation_id,
        details={
            "location": ".".join(
                str(item) for item in exc.errors()[0].get("loc", ())
            ),
            "reason": str(exc.errors()[0].get("type", "validation_error")),
        }
        if exc.errors()
        else {},
    )
    return JSONResponse(
        status_code=422,
        content=response.model_dump(mode="json"),
        headers={"X-API-Version": "v1", "Deprecation": "false"},
    )


async def _authenticate(
    runtime_provider: RuntimeProvider,
    request: Request,
    *,
    scopes: set[str],
    roles: set[PublicActorRole] | None = None,
) -> tuple[SleepApiRuntime, AuthenticatedActorContext]:
    runtime = runtime_provider()
    body = await request.body()
    context = runtime.authenticator.authenticate(
        request,
        body=body,
        required_scopes=frozenset(scopes),
        allowed_roles=None if roles is None else frozenset(roles),
    )
    return runtime, context


async def _command_auth(
    runtime_provider: RuntimeProvider,
    request: Request,
    *,
    scopes: set[str],
    roles: set[PublicActorRole],
) -> tuple[SleepApiRuntime, AuthenticatedActorContext, bytes]:
    runtime = runtime_provider()
    body = await request.body()
    if len(body) > 65_536:
        raise SleepApiApplicationError(
            PublicErrorCode.INVALID_REQUEST,
            "The command body exceeds the 64 KiB limit.",
            status_code=400,
        )
    context = runtime.authenticator.authenticate(
        request,
        body=body,
        required_scopes=frozenset(scopes),
        allowed_roles=frozenset(roles),
    )
    return runtime, context, body


def _require_context_scope(
    context: AuthenticatedActorContext,
    required_scope: str,
) -> None:
    if (
        required_scope not in context.claims.scope
        or required_scope not in context.binding.scopes
    ):
        raise SleepApiSecurityError(
            PublicErrorCode.AUTHORIZATION_DENIED,
            "The authenticated role does not grant this view.",
            status_code=403,
        )


def _idempotency_key(value: str | None) -> str:
    normalized = "" if value is None else value.strip()
    if not normalized or len(normalized) > 200:
        raise SleepApiApplicationError(
            PublicErrorCode.INVALID_REQUEST,
            "Idempotency-Key is required and must be at most 200 characters.",
            status_code=400,
        )
    return normalized


__all__ = [
    "create_sleep_api_router",
    "install_sleep_api",
    "sleep_api_error_handler",
]
