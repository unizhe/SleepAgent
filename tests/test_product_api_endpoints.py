from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit
from typing import Any

import backend.main as backend_main
from backend.main import app
from sleepagent.observability import STATE
from sleepagent.product_device import (
    DEFAULT_FAKE_RADAR_DEVICE_ID,
    FakeRadarProductDataProvider,
    LLM_NOT_CONFIGURED_MESSAGE,
    PRODUCT_RADAR_API_KEY_ENV,
    RadarDialogueStatus,
)
from sleepagent.radar_agent.replay import replay_scenario_ids
from sleepagent.radar_agent.product_agent import (
    CommunicationDraft,
    DeterministicCommitController,
    EpisodeStatus,
)
from sleepagent.radar_agent.product_agent.agents import ProductAgentFactory


PRODUCT_API_KEY = "test-product-radar-key"


def test_application_lifespan_starts_and_stops_product_induction(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        backend_main,
        "start_product_induction_worker",
        lambda: calls.append("induction:start"),
    )
    monkeypatch.setattr(
        backend_main,
        "stop_product_induction_worker",
        lambda: calls.append("induction:stop"),
    )

    async def exercise_lifespan() -> None:
        async with backend_main._application_lifespan(app):
            assert calls == ["induction:start"]

    asyncio.run(exercise_lifespan())

    assert calls == ["induction:start", "induction:stop"]


def test_product_radar_api_requires_auth(monkeypatch) -> None:
    _reset_fake_provider(monkeypatch)

    response = _request("GET", "/product/radar/devices")

    assert response["status"] == 401


def test_product_radar_api_requires_explicit_configured_auth_key(monkeypatch) -> None:
    monkeypatch.delenv(PRODUCT_RADAR_API_KEY_ENV, raising=False)
    monkeypatch.setattr(
        backend_main,
        "_RADAR_PRODUCT_PROVIDER",
        FakeRadarProductDataProvider(),
    )

    response = _request("GET", "/product/radar/devices")

    assert response["status"] == 503
    assert "not configured" in response["json"]["detail"]


def test_product_radar_api_auth_failure_log_is_redacted(
    monkeypatch,
    caplog,
) -> None:
    STATE.reset_for_tests()
    _reset_fake_provider(monkeypatch)
    caplog.set_level(logging.INFO, logger="sleepagent.observability")

    response = _request(
        "GET",
        "/product/radar/devices",
        headers={"Authorization": "Bearer wrong-secret-token"},
    )

    assert response["status"] == 401
    assert "product_api_auth_failure" in caplog.text
    assert "wrong-secret-token" not in caplog.text


def test_product_radar_fake_devices_dashboard_report_and_alerts_are_sanitized(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)

    devices_response = _request(
        "GET",
        "/product/radar/devices",
        headers=_api_key_headers(),
    )
    dashboard_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/dashboard",
        headers=_api_key_headers(),
    )
    report_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/sleep-report",
        headers=_api_key_headers(),
    )
    alerts_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/alerts",
        headers=_api_key_headers(),
    )

    assert devices_response["status"] == 200
    assert dashboard_response["status"] == 200
    assert report_response["status"] == 200
    assert alerts_response["status"] == 200

    devices = devices_response["json"]
    dashboard = dashboard_response["json"]
    report = report_response["json"]
    alerts = alerts_response["json"]

    assert devices[0]["radar_device_id"] == DEFAULT_FAKE_RADAR_DEVICE_ID
    assert dashboard["device"]["radar_device_id"] == DEFAULT_FAKE_RADAR_DEVICE_ID
    assert dashboard["current_snapshot"]["radar_device_id"] == DEFAULT_FAKE_RADAR_DEVICE_ID
    assert report["radar_device_id"] == DEFAULT_FAKE_RADAR_DEVICE_ID
    assert alerts[0]["radar_device_id"] == DEFAULT_FAKE_RADAR_DEVICE_ID
    for payload in (devices, dashboard, report, alerts):
        _assert_no_vendor_identifiers(payload)


def test_product_radar_chat_returns_llm_not_configured_without_source_metadata(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    response = _request(
        "POST",
        "/product/radar/chat",
        headers=_api_key_headers(),
        json_body={
            "radar_device_id": DEFAULT_FAKE_RADAR_DEVICE_ID,
            "user_message": "昨晚睡眠趋势怎么看？",
        },
    )

    assert response["status"] == 200
    payload = response["json"]
    assert payload["status"] == RadarDialogueStatus.LLM_NOT_CONFIGURED.value
    assert payload["assistant_message"] == LLM_NOT_CONFIGURED_MESSAGE
    _assert_no_vendor_identifiers(payload)


def test_retired_product_agent_run_routes_are_not_registered(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)
    response = _request(
        "POST",
        "/product/radar/agent-runs",
        headers={**_api_key_headers(), "Idempotency-Key": "runner-route"},
        json_body={
            "radar_device_id": DEFAULT_FAKE_RADAR_DEVICE_ID,
            "question": "昨晚睡得怎么样？",
            "role": "family",
            "idempotency_key": "runner-route",
        },
    )

    assert response["status"] == 404


def test_product_chat_uses_product_episode_runner_when_provider_is_configured(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)
    runner = _RecordingProductEpisodeRunner()
    monkeypatch.setattr(backend_main, "_PRODUCT_EPISODE_RUNNER", runner)
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ACTOR_ID", "actor-1")
    monkeypatch.setenv(
        "SLEEPAGENT_PRODUCT_SUBJECT_ID",
        DEFAULT_FAKE_RADAR_DEVICE_ID,
    )
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ACTOR_ROLE", "elder")

    response = _request(
        "POST",
        "/product/radar/chat",
        headers=_api_key_headers(),
        json_body={
            "radar_device_id": DEFAULT_FAKE_RADAR_DEVICE_ID,
            "user_message": "昨晚睡眠趋势怎么看？",
        },
    )

    assert response["status"] == 200
    assert response["json"]["status"] == RadarDialogueStatus.COMPLETED.value
    assert response["json"]["assistant_message"].startswith(
        "ProductEpisodeRunner"
    )
    assert len(runner.requests) == 1
    assert runner.requests[0].episode_type.value == "morning_review"
    assert len(runner.requests[0].runtime_readiness_decisions) == 7
    assert runner.requests[0].fact_snapshot.readiness_decision_refs


def test_configured_product_runner_fails_closed_without_actor_binding(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)
    monkeypatch.setattr(
        backend_main,
        "_PRODUCT_EPISODE_RUNNER",
        _RecordingProductEpisodeRunner(),
    )
    for name in (
        "SLEEPAGENT_PRODUCT_ACTOR_ID",
        "SLEEPAGENT_PRODUCT_SUBJECT_ID",
        "SLEEPAGENT_PRODUCT_ACTOR_ROLE",
    ):
        monkeypatch.delenv(name, raising=False)

    response = _request(
        "POST",
        "/product/radar/chat",
        headers=_api_key_headers(),
        json_body={
            "radar_device_id": DEFAULT_FAKE_RADAR_DEVICE_ID,
            "user_message": "昨晚睡眠趋势怎么看？",
        },
    )

    assert response["status"] == 503
    assert "actor binding is not configured" in response["json"]["detail"]


def test_product_radar_replay_scenario_catalog_is_exposed_by_api(monkeypatch) -> None:
    _reset_fake_provider(monkeypatch)

    list_response = _request(
        "GET",
        "/product/radar/replay-scenarios",
        headers=_api_key_headers(),
    )
    detail_response = _request(
        "GET",
        "/product/radar/replay-scenarios/escalate_candidate",
        headers=_api_key_headers(),
    )

    assert list_response["status"] == 200
    assert detail_response["status"] == 200
    assert [item["scenario_id"] for item in list_response["json"]] == list(replay_scenario_ids())
    assert detail_response["json"]["expected"]["risk_level"] == "escalate"
    assert "export_doctor_material" in detail_response["json"]["expected"]["confirmation_candidates"]


def test_product_radar_dashboard_uses_requested_replay_scenario(monkeypatch) -> None:
    _reset_fake_provider(monkeypatch)

    dashboard_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/dashboard?scenario=frequent_out_of_bed",
        headers=_api_key_headers(),
    )
    assert dashboard_response["status"] == 200
    assert dashboard_response["json"]["latest_sleep_report"]["getup_count"] == 5
    assert dashboard_response["json"]["data_quality"]["invalid_reading_count"] == 6


def test_product_radar_refresh_and_realtime_basic_flow_accepts_bearer_auth(
    monkeypatch,
) -> None:
    _reset_fake_provider(monkeypatch)

    refresh_response = _request(
        "POST",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/refresh",
        headers=_bearer_headers(),
    )
    initial_realtime_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/realtime",
        headers=_bearer_headers(),
    )
    start_realtime_response = _request(
        "POST",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/realtime/start",
        headers=_bearer_headers(),
    )
    active_realtime_response = _request(
        "GET",
        f"/product/radar/devices/{DEFAULT_FAKE_RADAR_DEVICE_ID}/realtime",
        headers=_bearer_headers(),
    )

    assert refresh_response["status"] == 200
    assert initial_realtime_response["status"] == 200
    assert start_realtime_response["status"] == 200
    assert active_realtime_response["status"] == 200

    refresh_payload = refresh_response["json"]
    initial_realtime = initial_realtime_response["json"]
    started_realtime = start_realtime_response["json"]
    active_realtime = active_realtime_response["json"]

    assert refresh_payload["dashboard"]["current_snapshot"] is not None
    assert initial_realtime["active"] is False
    assert started_realtime["active"] is True
    assert started_realtime["started_at"] is not None
    assert active_realtime["active"] is True
    assert active_realtime["latest_snapshot"] is not None
    for payload in (
        refresh_payload,
        initial_realtime,
        started_realtime,
        active_realtime,
    ):
        _assert_no_vendor_identifiers(payload)


def _reset_fake_provider(monkeypatch) -> None:
    monkeypatch.setenv(PRODUCT_RADAR_API_KEY_ENV, PRODUCT_API_KEY)
    monkeypatch.setattr(
        backend_main,
        "_RADAR_PRODUCT_PROVIDER",
        FakeRadarProductDataProvider(),
    )


class _ConfiguredModel:
    is_configured = True


class _RecordingProductEpisodeRunner:
    def __init__(self) -> None:
        model = _ConfiguredModel()
        self.agent_roster = ProductAgentFactory.create(
            sleepcare_model=model,
            evidence_reasoning_model=model,
            care_strategy_model=model,
            safety_review_model=model,
        )
        self.commit_controller = DeterministicCommitController()
        self.requests: list[Any] = []

    def run(self, request):
        self.requests.append(request)
        episode_id = request.episode_id
        return SimpleNamespace(
            publication=CommunicationDraft(
                draft_id=f"draft:{episode_id}",
                audience_role=(
                    request.audience_role
                    or request.fact_snapshot.binding.role
                ),
                text=f"ProductEpisodeRunner 已处理 {episode_id}",
                context_notice="已通过四角色发布门。",
            ),
            receipt=SimpleNamespace(
                episode_id=episode_id,
                status=EpisodeStatus.COMPLETE,
                failure_codes=[],
                safety_decision_refs=(
                    ["safety:doctor"] if request.doctor_material else []
                ),
                agent_invocation_ids=[f"invocation:{episode_id}"],
                tool_receipt_ids=[f"tool:{episode_id}"],
                accepted_work_product_refs=[f"work:{episode_id}"],
            ),
        )


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        _request_async(
            method,
            path,
            headers=headers or {},
            json_body=json_body,
        )
    )


async def _request_async(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
) -> dict[str, Any]:
    parsed_path = urlsplit(path)
    request_path = parsed_path.path
    query_string = parsed_path.query.encode("ascii")
    body = (
        json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if json_body is not None
        else b""
    )
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    if json_body is not None:
        raw_headers.append((b"content-type", b"application/json"))
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "path": request_path,
            "raw_path": request_path.encode("ascii"),
            "query_string": query_string,
            "headers": raw_headers,
            "client": ("testclient", 1),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    decoded_body = response_body.decode("utf-8")
    return {
        "status": status,
        "text": decoded_body,
        "json": json.loads(decoded_body) if decoded_body else None,
    }


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": PRODUCT_API_KEY}


def _bearer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {PRODUCT_API_KEY}"}


def _assert_no_vendor_identifiers(payload: Any) -> None:
    forbidden_keys = {
        "source_metadata",
        "vendor_device_id",
        "vendor_device_name",
        "vendor_home_id",
        "vendor_message_id",
        "vendor_product_id",
        "raw_payload",
        "data_payload",
        "device_name",
        "home_id",
    }
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in forbidden_keys
            _assert_no_vendor_identifiers(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_vendor_identifiers(item)
    else:
        text = str(payload)
        assert "imei-demo" not in text
        assert "vendor-device-demo" not in text
        assert "home-demo" not in text
