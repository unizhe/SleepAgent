from __future__ import annotations

import json
import logging

from sleepagent.observability import STATE, log_event


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
