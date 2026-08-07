from __future__ import annotations

from datetime import datetime
from typing import Any

from sleepagent.product_device import (
    DEFAULT_RADAR_VENDOR,
    RadarVitalSnapshot,
    RawVendorEvent,
    build_raw_vendor_event,
    build_vital_snapshot_from_raw_event,
)


def adapt_raw_vendor_event(
    payload: dict[str, Any],
    *,
    vendor: str = DEFAULT_RADAR_VENDOR,
    received_at: datetime | None = None,
    raw_event_id: str | None = None,
) -> RawVendorEvent:
    return build_raw_vendor_event(
        payload,
        vendor=vendor,
        received_at=received_at,
        raw_event_id=raw_event_id,
    )


def adapt_vital_snapshot(
    payload_or_event: dict[str, Any] | RawVendorEvent,
    *,
    radar_device_id: str | None = None,
    vendor: str = DEFAULT_RADAR_VENDOR,
    received_at: datetime | None = None,
) -> RadarVitalSnapshot:
    event = (
        payload_or_event
        if isinstance(payload_or_event, RawVendorEvent)
        else adapt_raw_vendor_event(
            payload_or_event,
            vendor=vendor,
            received_at=received_at,
        )
    )
    return build_vital_snapshot_from_raw_event(
        event,
        radar_device_id=radar_device_id,
    )


def extract_response_data(response: dict[str, Any]) -> Any:
    if str(response.get("code")) != "200" or response.get("success") is not True:
        message = response.get("message") or response.get("msg") or "Perceptor response failed."
        raise ValueError(str(message))
    return response.get("data")
