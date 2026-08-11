from datetime import datetime, timezone

import pytest

from sleepagent.product_device import (
    RadarBedPresence,
    RadarDevice,
    RadarDeviceStatus,
    RadarSourceMetadata,
    RawVendorEventNormalizationStatus,
    build_raw_vendor_event,
    build_vital_snapshot_from_raw_event,
    normalize_vendor_timestamp,
)


def test_vital_snapshot_marks_minus_one_heart_and_breath_missing() -> None:
    raw_event = build_raw_vendor_event(
        {
            "message_id": "msg-001",
            "product_id": "product-radar",
            "device_id": "vendor-device-id",
            "device_name": "imei-001",
            "home_id": 12345,
            "type": "VitalSignsDataEvent",
            "data": {
                "DateTime": 1_700_000_000_000,
                "HeartRate": -1,
                "BreathRate": -1,
                "BodyShake": 2,
                "OnBed": 1,
            },
        },
        received_at=datetime(2026, 7, 8, 1, 2, 3, tzinfo=timezone.utc),
    )

    snapshot = build_vital_snapshot_from_raw_event(
        raw_event,
        radar_device_id="radar-device-001",
    )

    assert snapshot.heart_rate_bpm is None
    assert snapshot.breath_rate_bpm is None
    assert snapshot.invalid_reading_flags == [
        "heart_rate_invalid_minus_one",
        "breath_rate_invalid_minus_one",
    ]
    assert snapshot.body_movement == 2
    assert snapshot.bed_presence == RadarBedPresence.IN_BED
    assert snapshot.measured_at == datetime.fromtimestamp(
        1_700_000_000,
        tz=timezone.utc,
    )
    assert snapshot.source_metadata.vendor_event_type == "VitalSignsDataEvent"
    assert snapshot.source_metadata.vendor_message_id == "msg-001"
    assert snapshot.source_metadata.vendor_device_id == "vendor-device-id"
    assert snapshot.source_metadata.vendor_device_name == "imei-001"
    assert snapshot.source_metadata.vendor_home_id == "12345"
    assert snapshot.source_metadata.raw_payload["type"] == "VitalSignsDataEvent"
    assert snapshot.source_metadata.data_payload["HeartRate"] == -1


def test_vital_snapshot_accepts_onbed_case_variant_and_json_string_data() -> None:
    raw_event = build_raw_vendor_event(
        {
            "messageId": "msg-002",
            "deviceName": "imei-002",
            "type": "VitalSignsDataEvent",
            "data": (
                '{"DateTime": 1700000000, "HeartRate": 68, '
                '"BreathRate": 15, "Onbed": 0}'
            ),
        }
    )

    snapshot = build_vital_snapshot_from_raw_event(raw_event)

    assert snapshot.radar_device_id == "imei-002"
    assert snapshot.heart_rate_bpm == 68
    assert snapshot.breath_rate_bpm == 15
    assert snapshot.bed_presence == RadarBedPresence.OUT_OF_BED
    assert snapshot.invalid_reading_flags == []
    assert snapshot.source_metadata.vendor_message_id == "msg-002"
    assert snapshot.source_metadata.data_payload["Onbed"] == 0


def test_timestamp_normalization_treats_seconds_and_milliseconds_equally() -> None:
    from_seconds = normalize_vendor_timestamp(1_700_000_000)
    from_milliseconds = normalize_vendor_timestamp(1_700_000_000_000)
    from_numeric_string = normalize_vendor_timestamp("1700000000000")

    assert from_seconds == from_milliseconds == from_numeric_string
    assert from_seconds.tzinfo == timezone.utc


def test_core_product_device_schemas_keep_source_metadata() -> None:
    source = RadarSourceMetadata(
        vendor="perceptor",
        vendor_event_type="ConnectedEvent",
        vendor_device_name="imei-003",
        raw_event_id="perceptor:ConnectedEvent:msg-003",
        raw_payload={"type": "ConnectedEvent", "device_name": "imei-003"},
    )
    device = RadarDevice(
        radar_device_id="radar-device-003",
        display_name="Bedroom radar",
        status=RadarDeviceStatus.ONLINE,
        vendor_device_name="imei-003",
        source_metadata=source,
    )
    assert device.source_metadata.device_identifier == "imei-003"
    assert device.source_metadata.raw_event_id == "perceptor:ConnectedEvent:msg-003"


def test_raw_vendor_event_rejects_non_object_data_string() -> None:
    with pytest.raises(ValueError, match="JSON must decode to an object"):
        build_raw_vendor_event(
            {
                "type": "VitalSignsDataEvent",
                "data": "[]",
            }
        )


def test_raw_vendor_event_defaults_to_raw_only_status() -> None:
    event = build_raw_vendor_event(
        {
            "type": "ConnectedEvent",
            "device_name": "imei-004",
        }
    )

    assert event.normalization_status == RawVendorEventNormalizationStatus.RAW_ONLY
    assert event.device_identifier == "imei-004"
