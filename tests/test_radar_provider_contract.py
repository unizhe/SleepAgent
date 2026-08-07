from __future__ import annotations

from typing import cast

import pytest

from sleepagent.radar_agent.provider import (
    FakeRadarProvider,
    ProviderAuthReservation,
    ProviderAuthenticationError,
    ProviderFaultState,
    ProviderPullCheckpoint,
    RadarProvider,
    ReplayRadarProvider,
)
from sleepagent.radar_agent.schemas import (
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
)


def test_replay_provider_implements_contract_and_returns_canonical_models() -> None:
    provider = cast(RadarProvider, ReplayRadarProvider())

    devices = provider.list_devices()
    device = provider.get_device(devices[0].radar_device_id)
    snapshots = provider.pull_snapshots(device.radar_device_id)
    night_report = provider.pull_night_report(device.radar_device_id)
    receipt = provider.receive_webhook({"message_id": "contract-event-001"})
    health = provider.health_check()

    assert isinstance(devices[0], RadarDevice)
    assert isinstance(device, RadarDevice)
    assert all(isinstance(snapshot, RadarVitalSnapshot) for snapshot in snapshots)
    assert isinstance(night_report, RadarNightSummary)
    assert receipt.accepted is True
    assert health.supported_methods == [
        "list_devices",
        "get_device",
        "pull_snapshots",
        "pull_night_report",
        "receive_webhook",
        "health_check",
    ]
    assert health.mode == "fake_replay"
    assert health.auth.scheme == "none"
    assert health.signature.header_name == "x-radar-provider-signature"
    assert health.timestamp_alignment.align_to_utc is True
    assert health.device_bindings[0].binding_status == "bound"
    assert health.device_bindings[0].subject_id == device.bound_subject_id
    assert health.fault_status.state == ProviderFaultState.AVAILABLE


def test_fake_radar_provider_alias_keeps_replay_contract_available() -> None:
    provider = FakeRadarProvider()

    assert isinstance(provider, ReplayRadarProvider)
    assert provider.list_devices()
    assert provider.health_check().mode == "fake_replay"


def test_replay_provider_webhook_receipt_is_idempotent() -> None:
    provider = ReplayRadarProvider()
    payload = {"message_id": "webhook-duplicate-001", "type": "VitalSignsDataEvent"}

    first = provider.receive_webhook(payload)
    second = provider.receive_webhook(payload)

    assert first.accepted is True
    assert first.duplicate is False
    assert first.idempotency_key == "webhook-duplicate-001"
    assert first.canonical_event_refs == ["webhook:webhook-duplicate-001"]
    assert second.accepted is True
    assert second.duplicate is True
    assert second.canonical_event_refs == []


def test_replay_provider_reserves_signed_webhook_verification() -> None:
    provider = ReplayRadarProvider(require_signed_webhooks=True)

    missing = provider.receive_webhook({"message_id": "signed-event-001"})
    accepted = provider.receive_webhook(
        {"message_id": "signed-event-002"},
        headers={"x-radar-provider-signature": "fake-signature"},
    )

    assert missing.accepted is False
    assert missing.signature.required is True
    assert missing.signature.verified is False
    assert "Missing required" in (missing.signature.failure_reason or "")
    assert accepted.accepted is True
    assert accepted.signature.required is True
    assert accepted.signature.verified is True


def test_replay_provider_reserves_auth_for_future_live_adapters() -> None:
    provider = ReplayRadarProvider(require_auth=True)

    with pytest.raises(ProviderAuthenticationError, match="auth is required"):
        provider.list_devices()

    auth = ProviderAuthReservation(required=True, scheme="api_key", key_id="test-key")
    assert provider.list_devices(auth=auth)
    assert provider.health_check().auth.required is True
    assert provider.health_check().auth.scheme == "api_key"


def test_replay_provider_supports_checkpointed_snapshot_pulls() -> None:
    provider = ReplayRadarProvider()
    device_id = provider.list_devices()[0].radar_device_id

    first_page = provider.pull_snapshots(device_id, limit=2)
    checkpoint = provider.last_pull_checkpoint
    assert isinstance(checkpoint, ProviderPullCheckpoint)
    assert checkpoint.has_more is True
    assert checkpoint.next_index == 2

    second_page = provider.pull_snapshots(device_id, cursor=checkpoint)
    first_ids = {snapshot.snapshot_id for snapshot in first_page}
    second_ids = {snapshot.snapshot_id for snapshot in second_page}

    assert first_page
    assert second_page
    assert first_ids.isdisjoint(second_ids)
    assert provider.last_pull_checkpoint is not None
    assert provider.last_pull_checkpoint.has_more is False
