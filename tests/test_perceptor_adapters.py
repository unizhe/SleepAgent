from datetime import datetime, timezone

import pytest

from sleepagent.integrations.perceptor.adapters import (
    adapt_raw_vendor_event,
    adapt_vital_snapshot,
    extract_response_data,
)
from sleepagent.product_device import RadarBedPresence


def test_adapter_converts_perceptor_vital_event_to_product_snapshot() -> None:
    payload = {
        "message_id": "msg-adapter-001",
        "device_id": "device-id-001",
        "device_name": "imei-adapter-001",
        "home_id": "home-001",
        "type": "VitalSignsDataEvent",
        "data": {
            "DateTime": 1_700_000_000_000,
            "HeartRate": 72,
            "BreathRate": 16,
            "Onbed": True,
        },
    }

    snapshot = adapt_vital_snapshot(
        payload,
        radar_device_id="radar-device-adapter-001",
        received_at=datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc),
    )

    assert snapshot.radar_device_id == "radar-device-adapter-001"
    assert snapshot.heart_rate_bpm == 72
    assert snapshot.breath_rate_bpm == 16
    assert snapshot.bed_presence == RadarBedPresence.IN_BED
    assert snapshot.source_metadata.vendor_message_id == "msg-adapter-001"
    assert snapshot.source_metadata.vendor_device_name == "imei-adapter-001"
    assert snapshot.source_metadata.data_payload["Onbed"] is True


def test_adapter_preserves_raw_vendor_event_for_non_vital_events() -> None:
    event = adapt_raw_vendor_event(
        {
            "messageId": "msg-connected-001",
            "deviceName": "imei-connected-001",
            "type": "ConnectedEvent",
            "data": "",
        }
    )

    assert event.event_type == "ConnectedEvent"
    assert event.message_id == "msg-connected-001"
    assert event.device_identifier == "imei-connected-001"
    assert event.raw_payload["type"] == "ConnectedEvent"


def test_extract_response_data_requires_successful_perceptor_response() -> None:
    assert extract_response_data({"code": 200, "success": True, "data": {"ok": True}}) == {
        "ok": True
    }

    with pytest.raises(ValueError, match="denied"):
        extract_response_data(
            {"code": 403, "success": False, "message": "denied", "data": None}
        )
