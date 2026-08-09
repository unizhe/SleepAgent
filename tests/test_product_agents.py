from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import sleepagent.product_device as product_device
import sleepagent.product_device.agents as retired_product_agents
import sleepagent.product_device.api as product_device_api
from sleepagent.product_device import (
    LLM_NOT_CONFIGURED_MESSAGE,
    OpenAICompatibleProviderConfig,
    PRODUCT_DIALOGUE_SCHEMA_VERSION,
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarBedPresence,
    RadarDashboardProjectionTool,
    RadarDevice,
    RadarDeviceStatus,
    RadarDialogueStatus,
    RadarProductDialogueRequest,
    RadarSleepReport,
    RadarSourceMetadata,
    RadarVitalSnapshot,
)
from sleepagent.product_device.agents import RadarProductAgent
from sleepagent.product_device.dialogue import (
    ProductDialogueAgent,
    ProductDialogueValidationError,
    build_product_dialogue_messages,
    validate_product_dialogue_json,
)


NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


class FakeProductProvider:
    is_configured = True

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []

    def create_json_completion(self, *, messages, config):
        self.calls.append({"messages": messages, "config": config})
        return json.dumps(self.payload, ensure_ascii=False)


def test_dashboard_projection_tool_marks_data_quality_caveats_and_blocks() -> None:
    device = _device(status=RadarDeviceStatus.OFFLINE)
    snapshots = [
        _snapshot(
            measured_at=NOW - timedelta(minutes=45),
            heart_rate_bpm=68,
            breath_rate_bpm=15,
            bed_presence=RadarBedPresence.IN_BED,
            raw_event_id="snapshot-old",
        ),
        _snapshot(
            measured_at=NOW - timedelta(minutes=20),
            heart_rate_bpm=None,
            breath_rate_bpm=None,
            bed_presence=RadarBedPresence.OUT_OF_BED,
            invalid_reading_flags=[
                "heart_rate_invalid_minus_one",
                "breath_rate_invalid_minus_one",
            ],
            raw_event_id="snapshot-current",
        ),
    ]
    report = _sleep_report(
        sleep_end_at=NOW - timedelta(days=3),
        raw_event_id="report-stale",
    )
    alert = _alert(raw_event_id="alert-001")

    dashboard = RadarDashboardProjectionTool().run(
        device=device,
        recent_snapshots=snapshots,
        latest_sleep_report=report,
        recent_alerts=[alert],
        now=NOW,
    )

    assert dashboard.current_snapshot == snapshots[-1]
    assert dashboard.data_quality.stale is True
    assert dashboard.data_quality.device_offline is True
    assert dashboard.data_quality.user_out_of_bed is True
    assert dashboard.data_quality.missing_intervals == 1
    assert dashboard.data_quality.missing_reading_count == 2
    assert dashboard.data_quality.invalid_reading_count == 2
    assert dashboard.data_quality.report_stale is True
    assert dashboard.data_quality.blocks_current_values is True
    assert "device_offline" in dashboard.blocked_reasons
    assert "user_out_of_bed" in dashboard.blocked_reasons
    assert any("invalid -1" in caveat for caveat in dashboard.caveats)
    assert "stale" in dashboard.summary_text.lower()


def test_retired_radar_product_agent_delegates_to_dashboard_tool_with_parity() -> None:
    inputs = {
        "device": _device(status=RadarDeviceStatus.ONLINE),
        "recent_snapshots": [
            _snapshot(
                measured_at=NOW - timedelta(seconds=30),
                heart_rate_bpm=64,
                breath_rate_bpm=15,
                bed_presence=RadarBedPresence.IN_BED,
            )
        ],
        "latest_sleep_report": _sleep_report(
            sleep_end_at=NOW - timedelta(hours=7)
        ),
        "recent_alerts": [_alert(raw_event_id="alert-parity")],
        "now": NOW,
    }

    canonical = RadarDashboardProjectionTool().run(**inputs)
    retired = RadarProductAgent().run(**inputs)

    assert retired.model_dump(mode="json") == canonical.model_dump(mode="json")


def test_product_device_package_and_api_do_not_expose_or_instantiate_old_agents() -> None:
    for retired_name in (
        "DeviceCareAgent",
        "ProductDialogueAgent",
        "RadarProductAgent",
        "RadarSleepAgentService",
    ):
        assert not hasattr(product_device, retired_name)

    assert not hasattr(retired_product_agents, "DeviceCareAgent")
    api_source = inspect.getsource(product_device_api)
    assert "product_device.agents" not in api_source
    assert "RadarProductAgent(" not in api_source


def test_product_dialogue_returns_not_configured_without_mock_answer() -> None:
    dashboard = _healthy_dashboard()
    request = RadarProductDialogueRequest(
        radar_device_id=dashboard.radar_device_id,
        user_message="昨晚睡眠趋势怎么看？",
        dashboard_summary=dashboard,
    )
    agent = ProductDialogueAgent(
        config=OpenAICompatibleProviderConfig(api_key_env="MISSING_PRODUCT_LLM_KEY")
    )

    result = agent.run(request)

    assert result.status == RadarDialogueStatus.LLM_NOT_CONFIGURED
    assert result.assistant_message == LLM_NOT_CONFIGURED_MESSAGE


def test_product_dialogue_blocks_current_question_when_quality_blocks_provider_call() -> None:
    poor_dashboard = RadarDashboardProjectionTool().run(
        device=_device(status=RadarDeviceStatus.OFFLINE),
        recent_snapshots=[
            _snapshot(
                measured_at=NOW - timedelta(minutes=20),
                heart_rate_bpm=None,
                breath_rate_bpm=None,
                bed_presence=RadarBedPresence.OUT_OF_BED,
                invalid_reading_flags=[
                    "heart_rate_invalid_minus_one",
                    "breath_rate_invalid_minus_one",
                ],
            )
        ],
        latest_sleep_report=_sleep_report(sleep_end_at=NOW - timedelta(days=3)),
        recent_alerts=[],
        now=NOW,
    )
    provider = FakeProductProvider(_valid_product_dialogue_payload())
    request = RadarProductDialogueRequest(
        radar_device_id=poor_dashboard.radar_device_id,
        user_message="现在设备心率和呼吸数据怎么看？",
        dashboard_summary=poor_dashboard,
    )

    result = ProductDialogueAgent(provider=provider).run(request)

    assert result.status == RadarDialogueStatus.BLOCKED
    assert provider.calls == []
    assert "device_offline" in result.blocked_reasons
    assert "data_quality_block" in result.safety_flags


def test_product_dialogue_uses_product_schema_prompt_and_provider_json() -> None:
    dashboard = _healthy_dashboard()
    provider = FakeProductProvider(_valid_product_dialogue_payload())
    request = RadarProductDialogueRequest(
        radar_device_id=dashboard.radar_device_id,
        user_message="昨晚在床和睡眠趋势有什么观察？",
        dashboard_summary=dashboard,
    )

    result = ProductDialogueAgent(provider=provider).run(request)

    assert result.status == RadarDialogueStatus.COMPLETED
    assert "在床" in result.assistant_message
    assert provider.calls
    user_prompt = provider.calls[0]["messages"][1]["content"]
    assert PRODUCT_DIALOGUE_SCHEMA_VERSION in user_prompt
    assert "stage7.llm_report_draft.v1" not in user_prompt
    assert "recent_observation_summary" in user_prompt
    assert "recent_snapshots" not in user_prompt
    assert "source_metadata" not in user_prompt


def test_product_dialogue_validator_rejects_clinical_medication_and_extra_fields() -> None:
    unsafe_payload = _valid_product_dialogue_payload()
    unsafe_payload["answer"] = "你已确诊睡眠呼吸暂停，需要开始用药。"

    with pytest.raises(ProductDialogueValidationError):
        validate_product_dialogue_json(unsafe_payload)

    extra_payload = _valid_product_dialogue_payload()
    extra_payload["diagnosis"] = "not allowed"
    with pytest.raises(ProductDialogueValidationError):
        validate_product_dialogue_json(extra_payload)


def test_product_dialogue_request_blocks_out_of_scope_and_clinical_questions() -> None:
    dashboard = _healthy_dashboard()
    agent = ProductDialogueAgent(
        provider=FakeProductProvider(_valid_product_dialogue_payload())
    )

    result = agent.run(
        RadarProductDialogueRequest(
            radar_device_id=dashboard.radar_device_id,
            user_message="能不能根据 PSG 诊断我有没有睡眠呼吸暂停？",
            dashboard_summary=dashboard,
        )
    )

    assert result.status == RadarDialogueStatus.BLOCKED
    assert "clinical_diagnosis_not_allowed" in result.blocked_reasons
    assert "clinical_sleep_test_claim_not_allowed" in result.blocked_reasons


def test_product_dialogue_prompt_builder_is_product_specific() -> None:
    dashboard = _healthy_dashboard()
    messages = build_product_dialogue_messages(
        RadarProductDialogueRequest(
            radar_device_id=dashboard.radar_device_id,
            user_message="告警是什么意思？",
            dashboard_summary=dashboard,
        )
    )

    assert messages[0]["role"] == "system"
    assert "radar product dialogue assistant" in messages[0]["content"]
    assert PRODUCT_DIALOGUE_SCHEMA_VERSION in messages[1]["content"]
    assert "stage7.llm_report_draft.v1" not in messages[1]["content"]


def test_product_radar_routes_are_registered() -> None:
    from backend.main import app

    route_paths = {route.path for route in app.routes}

    assert "/product/radar/devices" in route_paths
    assert "/product/radar/devices/{radar_device_id}/dashboard" in route_paths
    assert "/product/radar/devices/{radar_device_id}/refresh" in route_paths
    assert "/product/radar/devices/{radar_device_id}/realtime/start" in route_paths
    assert "/product/radar/devices/{radar_device_id}/realtime" in route_paths
    assert "/product/radar/devices/{radar_device_id}/sleep-report" in route_paths
    assert "/product/radar/devices/{radar_device_id}/alerts" in route_paths
    assert "/product/radar/chat" in route_paths


def _valid_product_dialogue_payload() -> dict:
    return {
        "schema_version": PRODUCT_DIALOGUE_SCHEMA_VERSION,
        "answer": "昨晚在床数据和睡眠报告显示可用于生活观察的趋势。",
        "observations": ["在床记录和睡眠报告已被参考。"],
        "suggested_actions": ["保持稳定作息，并继续观察连续几晚的变化。"],
        "caveats": ["雷达数据仅用于生活观察。"],
        "referenced_fields": ["dashboard_summary.trend_observations"],
    }


def _healthy_dashboard():
    return RadarDashboardProjectionTool().run(
        device=_device(status=RadarDeviceStatus.ONLINE),
        recent_snapshots=[
            _snapshot(
                measured_at=NOW - timedelta(seconds=60),
                heart_rate_bpm=62,
                breath_rate_bpm=14,
                bed_presence=RadarBedPresence.IN_BED,
            )
        ],
        latest_sleep_report=_sleep_report(sleep_end_at=NOW - timedelta(hours=8)),
        recent_alerts=[],
        now=NOW,
    )


def _device(*, status: RadarDeviceStatus) -> RadarDevice:
    source = _source("device-source")
    return RadarDevice(
        radar_device_id="radar-device-001",
        display_name="Bedroom radar",
        status=status,
        vendor_device_name="imei-001",
        source_metadata=source,
        updated_at=NOW,
    )


def _snapshot(
    *,
    measured_at: datetime,
    heart_rate_bpm: int | None,
    breath_rate_bpm: int | None,
    bed_presence: RadarBedPresence,
    invalid_reading_flags: list[str] | None = None,
    raw_event_id: str = "snapshot-source",
) -> RadarVitalSnapshot:
    return RadarVitalSnapshot(
        radar_device_id="radar-device-001",
        measured_at=measured_at,
        received_at=measured_at,
        heart_rate_bpm=heart_rate_bpm,
        breath_rate_bpm=breath_rate_bpm,
        body_movement=1,
        bed_presence=bed_presence,
        invalid_reading_flags=invalid_reading_flags or [],
        source_metadata=_source(raw_event_id),
    )


def _sleep_report(
    *,
    sleep_end_at: datetime,
    raw_event_id: str = "report-source",
) -> RadarSleepReport:
    return RadarSleepReport(
        radar_device_id="radar-device-001",
        report_date=sleep_end_at.date(),
        sleep_start_at=sleep_end_at - timedelta(hours=7),
        sleep_end_at=sleep_end_at,
        total_sleep_minutes=390,
        sleep_score=78,
        deep_sleep_minutes=80,
        light_sleep_minutes=230,
        rem_sleep_minutes=60,
        awake_minutes=20,
        movement_count=12,
        getup_count=1,
        source_metadata=_source(raw_event_id),
    )


def _alert(raw_event_id: str) -> RadarAlertEvent:
    return RadarAlertEvent(
        radar_alert_event_id="alert-001",
        radar_device_id="radar-device-001",
        alert_type="heart_rate",
        severity=RadarAlertSeverity.WARNING,
        occurred_at=NOW - timedelta(minutes=10),
        title="Heart rate alert",
        message="Vendor alert",
        source_metadata=_source(raw_event_id),
    )


def _source(raw_event_id: str) -> RadarSourceMetadata:
    return RadarSourceMetadata(
        vendor="perceptor",
        vendor_event_type="test_event",
        vendor_message_id=raw_event_id,
        vendor_device_name="imei-001",
        received_at=NOW,
        raw_event_id=raw_event_id,
        raw_payload={"id": raw_event_id},
    )
