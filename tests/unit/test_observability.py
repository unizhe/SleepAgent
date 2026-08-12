from __future__ import annotations

import json
import logging

from sleepagent.observability import (
    BACKEND_METRICS,
    STATE,
    backend_metrics_snapshot,
    log_event,
    record_backend_http,
    record_backend_queue,
    record_backend_signal,
)


def test_structured_log_redacts_secrets_and_omits_raw_payload(caplog) -> None:
    STATE.reset_for_tests()
    caplog.set_level(logging.INFO, logger="sleepagent.observability")

    log_event(
        "redaction_test",
        authorization="Bearer secret-token-value",
        api_key="secret-api-key-value",
        device_name="imei-secret-device",
        raw_payload={
            "message": "raw-secret-value",
            "nested": {"access_token": "nested-secret-token"},
        },
    )

    assert caplog.records
    record = json.loads(caplog.records[-1].message)
    serialized = caplog.text
    assert record["event"] == "redaction_test"
    assert record["authorization"] == "[redacted]"
    assert record["api_key"] == "[redacted]"
    assert record["device_name"].startswith("[hash:")
    assert record["raw_payload"]["omitted"] is True
    assert "secret-token-value" not in serialized
    assert "secret-api-key-value" not in serialized
    assert "imei-secret-device" not in serialized
    assert "raw-secret-value" not in serialized
    assert "nested-secret-token" not in serialized


def test_backend_metrics_have_bounded_non_phi_labels() -> None:
    BACKEND_METRICS.reset_for_tests()
    record_backend_http(
        method="GET",
        route_group="product_sleep",
        status_code=200,
        elapsed_ms=12,
    )
    record_backend_http(
        method="UNBOUNDED subject-123",
        route_group="/subject/secret",
        status_code=503,
        elapsed_ms=4,
    )
    record_backend_queue(queue="retention", outcome="succeeded")
    record_backend_signal(category="auth", outcome="denied")

    snapshot = backend_metrics_snapshot()

    assert snapshot["schema_version"] == "sleepagent_backend_metrics.v1"
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "subject-123" not in encoded
    assert "/subject/secret" not in encoded
    assert {item["route_group"] for item in snapshot["http_requests"]} == {
        "product_sleep",
        "unknown",
    }
    assert snapshot["signals"] == [
        {"category": "auth", "outcome": "denied", "count": 1}
    ]
