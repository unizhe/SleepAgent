import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sleepagent.integrations.perceptor import (
    PerceptorPushAuthenticationError,
    PerceptorPushConfigurationError,
    PerceptorPushRateLimitError,
    PerceptorPushReplayError,
    PerceptorPushRequestTooLarge,
    get_perceptor_push_runtime,
    read_bounded_starlette_request,
    start_perceptor_push_worker,
    stop_perceptor_push_worker,
)
from sleepagent.product_device import (
    PRODUCT_RADAR_API_KEY_ENV,
    FakeRadarProductDataProvider,
    LLM_NOT_CONFIGURED_MESSAGE,
    RadarDialogueStatus,
    RadarProductChatRequest,
    RadarPublicAlertEvent,
    RadarPublicDashboardSummary,
    RadarPublicDevice,
    RadarPublicDialogueResult,
    RadarPublicSleepReport,
    RadarRealtimeState,
    RadarRefreshResult,
    build_public_alert,
    build_public_dashboard,
    build_public_device,
    build_public_sleep_report,
)
from sleepagent.radar_agent.product_agent import (
    AgentId,
    AuthenticatedBinding,
    ClaimKind,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    ProductEpisodeRunRequest,
    ProductEpisodeRunner,
    ProductInductionScheduler,
    SourceScope,
    SourceScopeKind,
    build_unavailable_entry_decisions,
    build_product_episode_runner_from_env,
    product_episode_runner_is_configured,
    stable_hash,
    snapshot_binding_material,
)
from sleepagent.observability import (
    build_status_snapshot,
    log_event,
    record_error,
    record_push,
)
from sleepagent.radar_agent.replay import (
    ReplayScenario,
    ReplayScenarioSummary,
    get_replay_scenario,
    list_replay_scenarios,
)
from sleepagent.radar_agent.api.http import router as radar_agent_router
from sleepagent.radar_agent.product_agent.habit_api import (
    router as habit_profile_router,
)

DIAGNOSTIC_TRANSPORT_ENV = "SLEEPAGENT_RADAR_AGENT_DEV_MODE"
PRODUCT_PROVIDER_MODE_ENV = "SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE"
DEPLOYMENT_MODE_ENV = "SLEEPAGENT_DEPLOYMENT_MODE"


def _diagnostic_transport_enabled() -> bool:
    return (
        os.getenv(DIAGNOSTIC_TRANSPORT_ENV, "false").strip().lower() == "true"
        and os.getenv(DEPLOYMENT_MODE_ENV, "development").strip().lower()
        != "production"
    )


async def _require_diagnostic_transport(_request: Request) -> None:
    if not _diagnostic_transport_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@asynccontextmanager
async def _application_lifespan(_app: FastAPI):
    start_product_induction_worker()
    start_perceptor_push_worker()
    try:
        yield
    finally:
        stop_perceptor_push_worker()
        stop_product_induction_worker()


app = FastAPI(title="SleepAgent", version="0.1.0", lifespan=_application_lifespan)
app.include_router(
    radar_agent_router,
    dependencies=[Depends(_require_diagnostic_transport)],
)
app.include_router(habit_profile_router)
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:18510",
    "http://localhost:18510",
)
_RADAR_PRODUCT_PROVIDER: FakeRadarProductDataProvider | None = None
_PRODUCT_EPISODE_RUNNER: ProductEpisodeRunner | None = None
_PRODUCT_INDUCTION_SCHEDULER = ProductInductionScheduler(
    lambda: _PRODUCT_EPISODE_RUNNER,
)


def start_product_induction_worker() -> None:
    _PRODUCT_INDUCTION_SCHEDULER.start()


def stop_product_induction_worker() -> None:
    _PRODUCT_INDUCTION_SCHEDULER.stop()


def _product_episode_runner() -> ProductEpisodeRunner:
    global _PRODUCT_EPISODE_RUNNER
    if _PRODUCT_EPISODE_RUNNER is None:
        _PRODUCT_EPISODE_RUNNER = build_product_episode_runner_from_env()
    return _PRODUCT_EPISODE_RUNNER


def _product_agent_is_configured(runner: ProductEpisodeRunner) -> bool:
    return product_episode_runner_is_configured(runner)


def _require_product_actor_binding_configuration() -> tuple[str, str, str]:
    actor_id = os.getenv("SLEEPAGENT_PRODUCT_ACTOR_ID")
    subject_id = os.getenv("SLEEPAGENT_PRODUCT_SUBJECT_ID")
    role = os.getenv("SLEEPAGENT_PRODUCT_ACTOR_ROLE")
    if not actor_id or not subject_id or not role:
        raise HTTPException(
            status_code=503,
            detail=(
                "Product actor binding is not configured; "
                "SLEEPAGENT_PRODUCT_ACTOR_ID, "
                "SLEEPAGENT_PRODUCT_SUBJECT_ID and "
                "SLEEPAGENT_PRODUCT_ACTOR_ROLE are required."
            ),
        )
    if role not in {"elder", "family", "doctor"}:
        raise HTTPException(status_code=503, detail="Product actor role is invalid.")
    return actor_id, subject_id, role


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "SLEEPAGENT_CORS_ORIGINS",
            ",".join(DEFAULT_CORS_ORIGINS),
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _require_product_radar_auth(request: Request) -> None:
    await _require_diagnostic_transport(request)
    expected_token = os.getenv(PRODUCT_RADAR_API_KEY_ENV)
    if not expected_token:
        context = _product_auth_context(request)
        log_event(
            "product_api_auth_not_configured",
            level=logging.ERROR,
            source="product_api",
            **context,
        )
        record_error(
            event="product_api_auth_not_configured",
            error=f"{PRODUCT_RADAR_API_KEY_ENV} is required.",
            source="product_api",
            context=context,
        )
        raise HTTPException(
            status_code=503,
            detail="Product radar API authentication is not configured.",
        )
    provided_token = _extract_product_radar_auth_token(request)
    if provided_token != expected_token:
        context = _product_auth_context(request)
        log_event(
            "product_api_auth_failure",
            level=logging.WARNING,
            source="product_api",
            **context,
        )
        record_error(
            event="product_api_auth_failure",
            error="Product radar API authentication failed.",
            source="product_api",
            context=context,
        )
        raise HTTPException(
            status_code=401,
            detail="Product radar API authentication required.",
        )


def _extract_product_radar_auth_token(request: Request) -> str | None:
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()

    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@app.get("/health")
async def health_check() -> dict[str, Any]:
    return _status_payload()


@app.get("/status")
async def status_check() -> dict[str, Any]:
    return _status_payload()


@app.get("/product/radar/devices", response_model=list[RadarPublicDevice])
async def list_radar_product_devices(
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> list[RadarPublicDevice]:
    return [
        build_public_device(device)
        for device in _radar_product_data_provider(scenario).list_devices()
    ]


@app.get(
    "/product/radar/replay-scenarios",
    response_model=list[ReplayScenarioSummary],
)
async def list_radar_replay_scenarios(
    _: None = Depends(_require_product_radar_auth),
) -> list[ReplayScenarioSummary]:
    return list_replay_scenarios()


@app.get(
    "/product/radar/replay-scenarios/{scenario_id}",
    response_model=ReplayScenario,
)
async def get_radar_replay_scenario(
    scenario_id: str,
    _: None = Depends(_require_product_radar_auth),
) -> ReplayScenario:
    try:
        return get_replay_scenario(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Replay scenario not found.") from exc


@app.get(
    "/product/radar/devices/{radar_device_id}/dashboard",
    response_model=RadarPublicDashboardSummary,
)
async def get_radar_product_dashboard(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarPublicDashboardSummary:
    try:
        dashboard = _radar_product_data_provider(scenario).build_dashboard(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc
    return build_public_dashboard(dashboard)


@app.post(
    "/product/radar/devices/{radar_device_id}/refresh",
    response_model=RadarRefreshResult,
)
async def refresh_radar_product_device(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarRefreshResult:
    try:
        dashboard = _radar_product_data_provider(scenario).refresh_device(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc
    return RadarRefreshResult(
        radar_device_id=radar_device_id,
        refreshed_at=dashboard.generated_at,
        dashboard=build_public_dashboard(dashboard),
        message="Radar device refreshed with fake provider data.",
    )


@app.post(
    "/product/radar/devices/{radar_device_id}/realtime/start",
    response_model=RadarRealtimeState,
)
async def start_radar_product_realtime(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarRealtimeState:
    try:
        return _radar_product_data_provider(scenario).start_realtime(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc


@app.get(
    "/product/radar/devices/{radar_device_id}/realtime",
    response_model=RadarRealtimeState,
)
async def get_radar_product_realtime(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarRealtimeState:
    try:
        return _radar_product_data_provider(scenario).get_realtime(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc


@app.get(
    "/product/radar/devices/{radar_device_id}/sleep-report",
    response_model=RadarPublicSleepReport,
)
async def get_radar_product_sleep_report(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarPublicSleepReport:
    try:
        report = _radar_product_data_provider(scenario).get_latest_sleep_report(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="Radar sleep report not found.")
    return build_public_sleep_report(report)


@app.get(
    "/product/radar/devices/{radar_device_id}/alerts",
    response_model=list[RadarPublicAlertEvent],
)
async def get_radar_product_alerts(
    radar_device_id: str,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> list[RadarPublicAlertEvent]:
    try:
        alerts = _radar_product_data_provider(scenario).get_recent_alerts(radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc
    return [build_public_alert(alert) for alert in alerts]


@app.post("/product/radar/chat", response_model=RadarPublicDialogueResult)
async def radar_product_chat(
    request: RadarProductChatRequest,
    scenario: Annotated[
        str | None,
        Query(description="Optional deterministic replay scenario id."),
    ] = None,
    _: None = Depends(_require_product_radar_auth),
) -> RadarPublicDialogueResult:
    provider = _radar_product_data_provider(scenario)
    try:
        dashboard = provider.build_dashboard(request.radar_device_id)
        snapshots = provider.get_recent_snapshots(request.radar_device_id)
        alerts = provider.get_recent_alerts(request.radar_device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Radar device not found.") from exc
    runner = _product_episode_runner()
    if not _product_agent_is_configured(runner):
        return RadarPublicDialogueResult(
            radar_device_id=request.radar_device_id,
            status=RadarDialogueStatus.LLM_NOT_CONFIGURED,
            assistant_message=LLM_NOT_CONFIGURED_MESSAGE,
            caveats=["四角色 ProductEpisodeRunner 未调用未配置的模型。"],
            generated_at=datetime.now(timezone.utc),
        )
    timezone_name = os.getenv(
        "SLEEPAGENT_PRODUCT_TIMEZONE",
        "Asia/Shanghai",
    )
    now = datetime.now(timezone.utc)
    source_as_of = dashboard.generated_at
    local_date = source_as_of.astimezone(ZoneInfo(timezone_name)).date()
    actor_id, subject_id, role = _require_product_actor_binding_configuration()
    source_ref = (
        f"product-radar:{request.radar_device_id}:"
        f"{dashboard.generated_at.isoformat()}"
    )
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=source_as_of,
        timezone_name=timezone_name,
        date_start=local_date,
        date_end=local_date,
        # This dialogue can discuss several metrics, so the legacy scalar is
        # deliberately non-authoritative.
        valid_night_count=0,
    )
    readiness_decisions = build_unavailable_entry_decisions(
        decision_namespace=(
            f"product-chat:{stable_hash((request.radar_device_id, now.isoformat()))[:20]}"
        ),
        claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
    )
    fact_snapshot = FactSnapshot.create(
        fact_snapshot_id=(
            f"product-chat:{stable_hash((request.radar_device_id, now.isoformat()))[:20]}"
        ),
        binding=AuthenticatedBinding(
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            authorization_scope=(
                "read_sleep_data",
                "read_device_data",
                "draft_material",
            ),
        ),
        source_scope=scope,
        canonical_data_version=stable_hash(
            {
                "dashboard": dashboard.model_dump(mode="json"),
                "snapshots": [
                    item.model_dump(mode="json") for item in snapshots
                ],
                "alerts": [item.model_dump(mode="json") for item in alerts],
            }
        ),
        care_context_version=runner.commit_controller.care_store.get(
            subject_id
        ).version,
        memory_context_version=runner.commit_controller.memory_store.get(
            subject_id
        ).version,
        source_refs=(source_ref,),
        **snapshot_binding_material(decisions=readiness_decisions),
        created_at=now,
    )
    episode_result = runner.run(
        ProductEpisodeRunRequest(
            episode_id=f"api:{stable_hash((actor_id, request.user_message, now.isoformat()))[:24]}",
            episode_type=EpisodeType.MORNING_REVIEW,
            objective="基于已授权雷达信息回答当前用户问题",
            fact_snapshot=fact_snapshot,
            runtime_readiness_decisions=readiness_decisions,
            user_text=request.user_message,
            personalized=True,
            tool_inputs={
                "radar.get_night_evidence": {
                    "data": {
                        "dashboard": dashboard.model_dump(mode="json"),
                        "recent_snapshots": [
                            item.model_dump(mode="json") for item in snapshots
                        ],
                        "recent_alerts": [
                            item.model_dump(mode="json") for item in alerts
                        ],
                    },
                    "source_refs": [source_ref],
                },
                "radar.assess_data_quality": {
                    "coverage_ratio": (
                        0.0
                        if dashboard.data_quality.blocks_current_values
                        else 1.0
                    ),
                    "source_refs": [source_ref],
                },
            },
        )
    )
    publication = episode_result.publication
    status = (
        RadarDialogueStatus.BLOCKED
        if episode_result.receipt.status == EpisodeStatus.BLOCKED
        else (
            RadarDialogueStatus.COMPLETED
            if publication is not None
            else RadarDialogueStatus.FAILED
        )
    )
    return RadarPublicDialogueResult(
        radar_device_id=request.radar_device_id,
        status=status,
        assistant_message=(
            publication.text
            if publication is not None
            else "当前无法可靠完成这次解释，请稍后重试。"
        ),
        safety_flags=(
            ["safety_reviewed"]
            if episode_result.receipt.safety_decision_refs
            else []
        ),
        blocked_reasons=episode_result.receipt.failure_codes,
        caveats=(
            [publication.context_notice]
            if publication is not None
            else ["四角色运行时已安全降级。"]
        ),
        generated_at=now,
    )


@app.post("/integrations/perceptor/webhook")
async def perceptor_webhook(
    request: Request,
) -> dict[str, object]:
    log_event(
        "webhook_ingestion_received",
        source="perceptor_webhook",
        method=request.method,
        path=request.url.path,
    )
    try:
        runtime = get_perceptor_push_runtime()
        raw_request = await read_bounded_starlette_request(
            request,
            limits=runtime.ingestion.http_limits,
        )
        result = runtime.ingestion.ingest(
            raw_request,
            received_at=datetime.now(timezone.utc),
        )
    except PerceptorPushRequestTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except PerceptorPushRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PerceptorPushAuthenticationError as exc:
        log_event(
            "webhook_ingestion_rejected",
            level=logging.WARNING,
            source="perceptor_webhook",
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PerceptorPushReplayError as exc:
        log_event(
            "webhook_ingestion_replay_rejected",
            level=logging.WARNING,
            source="perceptor_webhook",
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PerceptorPushConfigurationError as exc:
        log_event(
            "webhook_ingestion_configuration_error",
            level=logging.ERROR,
            source="perceptor_webhook",
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        record_error(
            event="webhook_ingestion_storage_error",
            error=exc,
            source="perceptor_webhook",
            context={"path": request.url.path},
        )
        raise HTTPException(
            status_code=503,
            detail="Perceptor push could not be durably committed",
        ) from exc

    record_push(source="perceptor_webhook", event_type=result.event_type)
    log_event(
        "webhook_ingestion_completed",
        source="perceptor_webhook",
        event_type=result.event_type,
        duplicate=result.duplicate,
        collision=result.collision,
        normalization_status=result.normalization_status,
    )
    return result.to_response_payload()


def _radar_product_data_provider(
    scenario: str | None = None,
) -> FakeRadarProductDataProvider:
    global _RADAR_PRODUCT_PROVIDER
    if (
        not _diagnostic_transport_enabled()
        or os.getenv(PRODUCT_PROVIDER_MODE_ENV, "").strip().lower() != "fake"
    ):
        raise RuntimeError(
            "FakeRadarProductDataProvider requires explicit development/test "
            "mode and SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE=fake"
        )
    if scenario is not None:
        if (
            _RADAR_PRODUCT_PROVIDER is not None
            and getattr(_RADAR_PRODUCT_PROVIDER, "scenario_id", None) == scenario
        ):
            return _RADAR_PRODUCT_PROVIDER
        return FakeRadarProductDataProvider(scenario)
    if _RADAR_PRODUCT_PROVIDER is None:
        _RADAR_PRODUCT_PROVIDER = FakeRadarProductDataProvider()
    return _RADAR_PRODUCT_PROVIDER


def _status_payload() -> dict[str, Any]:
    if (
        _diagnostic_transport_enabled()
        and os.getenv(PRODUCT_PROVIDER_MODE_ENV, "").strip().lower() == "fake"
    ):
        _refresh_product_data_freshness_for_status()
    return build_status_snapshot()


def _refresh_product_data_freshness_for_status() -> None:
    try:
        provider = _radar_product_data_provider()
        devices = provider.list_devices()
        if devices:
            provider.build_dashboard(devices[0].radar_device_id)
    except Exception as exc:  # pragma: no cover - health must stay best effort.
        log_event(
            "status_data_freshness_error",
            level=logging.WARNING,
            source="status",
            error_type=exc.__class__.__name__,
        )
        record_error(
            event="status_data_freshness_error",
            error=exc,
            source="status",
        )


def _product_auth_context(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("authorization")
    scheme = authorization.partition(" ")[0] if authorization else None
    return {
        "method": request.method,
        "path": str(request.scope.get("path") or ""),
        "client_host": request.client.host if request.client else None,
        "key_header_present": bool(request.headers.get("x-api-key")),
        "auth_scheme": scheme,
        "auth_header_present": bool(authorization),
    }
