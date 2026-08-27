from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from sleepagent.domain.contracts import (
    AlertLifecycleState,
    AlertSeverity,
    BedPresencePayload,
    BedPresenceState,
    DataMode,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationType,
    RespiratoryRatePayload,
    TimezoneStatus,
    VendorAlertPayload,
)
from sleepagent.integrations.perceptor.push import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    AlarmEvent,
    AlarmStopEvent,
    DataRepresentation,
    DuplicateJsonKeyError,
    FieldState,
    PushContractError,
    UnsupportedPushContractError,
    VitalSignsDataEvent,
    normalize_push_envelope,
    parse_push_envelope,
    verify_push_envelope_signature,
)
from sleepagent.integrations.perceptor.signing import (
    PLATFORM_SIGNING_PATH,
    PLATFORM_SIGNING_KEY_MODE,
    PLATFORM_SIGNING_KEY_STATUS,
    PLATFORM_SIGNATURE_GOLDEN_STATUS,
    PUSH_SIGNING_PATH,
    PUSH_SIGNING_KEY_MODE,
    PUSH_SIGNATURE_GOLDEN_STATUS,
    SigningContractError,
    SigningKeyMode,
    SigningRepresentationUnresolved,
    build_canonical_query_string,
    build_string_to_sign,
    percent_encode,
    sign_parameters,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "perceptor_v2_5_2"
RECEIVED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "synthetic-contract-account"
SYNTHETIC_SECRET = "synthetic-contract-secret"
SYNTHETIC_PUSH_GOLDEN_SECRET = "synthetic-push-golden-secret"
SYNTHETIC_PUSH_GOLDEN_SIGNATURE = "q8m0jqDS6AguJ/P8VGpBmxA17Sk="
SYNTHETIC_PUSH_GOLDEN_PARAMETERS: dict[str, object] = {
    "client_id": "synthetic-push-client",
    "version": "2.0",
    "timestamp": 1767225600,
    "sign_version": "2.0",
    "sign_nonce": "synthetic-push-nonce-0001",
    "sign_method": "HMAC-SHA1",
    "message_id": "synthetic-push-message-0001",
    "product_id": 7000000000000000001,
    "device_id": 7000000000000000002,
    "device_name": "SYNTHETIC-PUSH-DEVICE",
    "home_id": 7000000000000000003,
    "type": "VitalSignsDataEvent",
    "data": (
        '{"HeartRate":70,"BreathRate":16,"BodyShake":1,"OnBed":1,'
        '"ReportTime":"1767225600000",'
        '"DateTime":"2026-01-01T00:00:00.000"}'
    ),
}


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _payload(name: str = "vital_signs_data_event.json") -> dict[str, Any]:
    value = json.loads(_fixture(name))
    assert isinstance(value, dict)
    return value


def _raw(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize(raw: bytes):
    return normalize_push_envelope(
        parse_push_envelope(raw),
        provider_account_id=ACCOUNT_ID,
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )


def _vital_raw(**changes: object) -> bytes:
    payload = _payload()
    data = dict(payload["data"])
    data.update(changes)
    payload["data"] = data
    return _raw(payload)


def test_fixture_manifest_keeps_documented_and_real_evidence_distinct() -> None:
    manifest = json.loads(_fixture("manifest.json"))
    assert set(manifest["fixtures"].values()) == {
        "DOCUMENTED_FIXTURE",
        "SANITIZED_RECORDED_REAL_FIXTURE",
        "SANITIZED_RECORDED_REAL_PULL_FIXTURE",
    }
    assert "RECORDED_REAL_PAYLOAD" not in set(manifest["fixtures"].values())
    assert sorted(manifest["fixtures"]) == sorted(
        path.name for path in FIXTURES.glob("*.json") if path.name != "manifest.json"
    )


def test_sanitized_recorded_real_vital_structure_is_supported_and_typed() -> None:
    raw = _fixture("sanitized_recorded_real_vital.json")
    envelope = parse_push_envelope(raw)
    assert envelope.data_representation == DataRepresentation.ESCAPED_JSON_STRING
    assert isinstance(envelope.timestamp, int)
    assert isinstance(envelope.event, VitalSignsDataEvent)
    assert envelope.event.onbed.name == "OnBed"
    assert envelope.event.onbed.state == FieldState.PRESENT
    assert envelope.event.report_time.state == FieldState.PRESENT
    assert isinstance(envelope.event.report_time.value, str)
    assert envelope.event.date_time.state == FieldState.PRESENT
    assert isinstance(envelope.event.date_time.value, str)
    candidates = normalize_push_envelope(
        envelope,
        provider_account_id=ACCOUNT_ID,
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )
    assert len(candidates) == 4
    assert all(
        item.provenance.raw_payload_sha256 == envelope.raw_payload_sha256
        for item in candidates
    )
    candidate_json = json.dumps(
        [item.model_dump(mode="json") for item in candidates],
        sort_keys=True,
    )
    assert "DateTime" not in candidate_json
    assert "OnBed" not in candidate_json
    assert "SANITIZED_RECORDED_REAL_FIXTURE" in _fixture("manifest.json").decode()


def test_sanitized_recorded_real_replay_is_deterministic() -> None:
    raw = _fixture("sanitized_recorded_real_vital.json")
    first_envelope = parse_push_envelope(raw)
    second_envelope = parse_push_envelope(raw)
    first_candidates = normalize_push_envelope(
        first_envelope,
        provider_account_id=ACCOUNT_ID,
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )
    second_candidates = normalize_push_envelope(
        second_envelope,
        provider_account_id=ACCOUNT_ID,
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )
    assert first_envelope.raw_payload_sha256 == second_envelope.raw_payload_sha256
    assert first_envelope.idempotency_identity == second_envelope.idempotency_identity
    assert first_candidates == second_candidates
    assert [item.candidate_id for item in first_candidates] == [
        item.candidate_id for item in second_candidates
    ]
    assert [item.idempotency_key for item in first_candidates] == [
        item.idempotency_key for item in second_candidates
    ]


def test_vital_onbed_casing_is_fail_closed_when_both_forms_are_present() -> None:
    payload = _payload()
    data = dict(payload["data"])
    data["OnBed"] = data["Onbed"]
    payload["data"] = data
    with pytest.raises(UnsupportedPushContractError, match="ambiguous"):
        parse_push_envelope(_raw(payload))


def test_documented_platform_signing_vector() -> None:
    params = {
        "client_id": "contract-client",
        "device_name": "设备 A/B",
        "empty": "",
        "sign": "excluded",
        "timestamp": 1723193640,
        "version": "2.0",
    }
    assert build_canonical_query_string(params) == (
        "client_id=contract-client&"
        "device_name=%E8%AE%BE%E5%A4%87%20A%2FB&"
        "empty=&timestamp=1723193640&version=2.0"
    )
    assert build_string_to_sign(params) == (
        "POST&%2Fv2&client_id%3Dcontract-client%26"
        "device_name%3D%25E8%25AE%25BE%25E5%25A4%2587%2520A%252FB%26"
        "empty%3D%26timestamp%3D1723193640%26version%3D2.0"
    )
    assert sign_parameters(params, client_secret=SYNTHETIC_SECRET) == (
        "3JRTmAtamCdahqqeNopxCoO7VE4="
    )
    assert PLATFORM_SIGNING_PATH == "/v2"


def test_percent_encoding_uses_rfc3986_not_form_encoding() -> None:
    assert percent_encode("a b+c/*~") == "a%20b%2Bc%2F%2A~"
    assert "+" not in percent_encode("a b")


def test_sign_is_excluded_and_sorting_is_deterministic() -> None:
    first = {"z": "last", "sign": "one", "a": "first"}
    second = {"a": "first", "z": "last", "sign": "two"}
    assert build_canonical_query_string(first) == "a=first&z=last"
    assert sign_parameters(first, client_secret=SYNTHETIC_SECRET) == sign_parameters(
        second,
        client_secret=SYNTHETIC_SECRET,
    )


@pytest.mark.parametrize("value", [None, True, ["device-1"], {"x": 1}])
def test_unresolved_signing_representations_fail_closed(value: object) -> None:
    with pytest.raises(SigningRepresentationUnresolved):
        build_canonical_query_string({"value": value})


def test_push_and_platform_key_deltas_are_explicit_separate_contracts() -> None:
    params = {"client_id": "synthetic", "timestamp": 1}
    documented = sign_parameters(params, client_secret=SYNTHETIC_SECRET)
    ampersand = sign_parameters(
        params,
        client_secret=SYNTHETIC_SECRET,
        key_mode=SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND,
    )
    assert documented != ampersand
    assert PUSH_SIGNING_KEY_MODE == SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND
    assert PUSH_SIGNATURE_GOLDEN_STATUS == "VERIFIED_REAL_CONTRACT_DELTA"
    assert PLATFORM_SIGNING_KEY_MODE == SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND
    assert PLATFORM_SIGNING_KEY_STATUS == "VERIFIED_REAL_CONTRACT_DELTA"
    assert PLATFORM_SIGNATURE_GOLDEN_STATUS == "VERIFIED_REAL_CONTRACT_DELTA"


def test_undocumented_signing_path_and_key_mode_fail_closed() -> None:
    with pytest.raises(SigningContractError, match="signing path"):
        sign_parameters(
            {"client_id": "synthetic"},
            client_secret=SYNTHETIC_SECRET,
            signing_path="/callback",
        )
    with pytest.raises(SigningContractError, match="key mode"):
        sign_parameters(
            {"client_id": "synthetic"},
            client_secret=SYNTHETIC_SECRET,
            key_mode=cast(SigningKeyMode, "unknown-mode"),
        )


def test_signing_errors_and_results_do_not_expose_secret() -> None:
    signature = sign_parameters(
        {"client_id": "synthetic"},
        client_secret=SYNTHETIC_SECRET,
    )
    assert SYNTHETIC_SECRET not in signature
    with pytest.raises(SigningContractError) as caught:
        sign_parameters({}, client_secret="")
    assert SYNTHETIC_SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("fixture_name", "event_type"),
    [
        ("vital_signs_data_event.json", "VitalSignsDataEvent"),
        ("alarm_event.json", "AlarmEvent"),
        ("alarm_stop_event.json", "AlarmStopEvent"),
        ("connected_event.json", "ConnectedEvent"),
        ("disconnected_event.json", "DisconnectedEvent"),
    ],
)
def test_documented_fixtures_parse_deterministically(
    fixture_name: str,
    event_type: str,
) -> None:
    raw = _fixture(fixture_name)
    first = parse_push_envelope(raw)
    second = parse_push_envelope(raw)
    assert first == second
    assert first.event_type == event_type
    assert first.raw_body == raw
    assert first.raw_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.product_id == "1000000000000000001"
    assert first.device_name == "DOCUMENTED-DEVICE-PLACEHOLDER"
    assert first.home_id == "1000000000000000003"


def test_parser_preserves_absent_null_empty_and_present() -> None:
    payload = _payload()
    data = dict(payload["data"])
    data.pop("HeartRate")
    data["BreathRate"] = None
    data["BodyShake"] = ""
    payload["data"] = data
    envelope = parse_push_envelope(_raw(payload))
    assert isinstance(envelope.event, VitalSignsDataEvent)
    assert envelope.event.heart_rate.state == FieldState.ABSENT
    assert envelope.event.breath_rate.state == FieldState.NULL
    assert envelope.event.body_shake.state == FieldState.EMPTY
    assert envelope.event.onbed.state == FieldState.PRESENT


def test_duplicate_keys_are_rejected_at_outer_and_nested_levels() -> None:
    with pytest.raises(DuplicateJsonKeyError):
        parse_push_envelope(
            b'{"client_id":"a","client_id":"b"}'
        )
    payload = _payload("alarm_event.json")
    payload["data"] = '{"AlarmId":"one","AlarmId":"two"}'
    with pytest.raises(DuplicateJsonKeyError):
        parse_push_envelope(_raw(payload))


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b"\xff",
        b'{"client_id":NaN}',
    ],
)
def test_malformed_or_non_object_json_fails_closed(raw: bytes) -> None:
    with pytest.raises(PushContractError):
        parse_push_envelope(raw)


@pytest.mark.parametrize("data", [42, [], "[]", "not-json", ""])
def test_invalid_vital_data_shapes_fail_closed(data: object) -> None:
    payload = _payload()
    payload["data"] = data
    with pytest.raises(PushContractError):
        parse_push_envelope(_raw(payload))


def test_object_form_data_is_restricted_to_documented_vital_example() -> None:
    payload = _payload("alarm_event.json")
    payload["data"] = {
        "AlarmId": "DOCUMENTED-ALARM-PLACEHOLDER",
        "AlarmLevel": 2,
        "value": "107",
        "AlarmTStamp": "2024-08-09T16:54:00.002",
    }
    with pytest.raises(UnsupportedPushContractError, match="object-form"):
        parse_push_envelope(_raw(payload))


def test_missing_required_device_identity_fails_closed() -> None:
    payload = _payload()
    payload.pop("device_name")
    with pytest.raises(PushContractError, match="device_name"):
        parse_push_envelope(_raw(payload))


def test_present_message_id_must_be_a_scalar_identifier() -> None:
    payload = _payload()
    payload["message_id"] = {"not": "an identifier"}
    with pytest.raises(PushContractError, match="message_id"):
        parse_push_envelope(_raw(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "UnknownVendorEvent"),
        ("version", "3.0"),
        ("sign_version", "1.0"),
        ("sign_method", "HMAC-SHA256"),
    ],
)
def test_unknown_event_or_unsupported_signing_contract_fails_closed(
    field: str,
    value: str,
) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(UnsupportedPushContractError):
        parse_push_envelope(_raw(payload))


def test_unknown_event_can_be_parsed_only_for_authenticated_raw_quarantine() -> None:
    payload = _payload()
    payload["type"] = "FutureVendorEvent"
    payload["data"] = '{"FutureField":"opaque"}'
    envelope = parse_push_envelope(
        _raw(payload),
        allow_unknown_event=True,
    )
    assert envelope.event_type == "FutureVendorEvent"


def test_unknown_top_level_or_event_field_fails_closed() -> None:
    payload = _payload()
    payload["unexpected"] = "value"
    with pytest.raises(UnsupportedPushContractError):
        parse_push_envelope(_raw(payload))
    payload = _payload()
    data = dict(payload["data"])
    data["Unexpected"] = 1
    payload["data"] = data
    with pytest.raises(UnsupportedPushContractError):
        parse_push_envelope(_raw(payload))


def test_push_string_data_signature_verifies_with_frozen_real_contract() -> None:
    payload = _payload("alarm_event.json")
    payload.pop("sign")
    payload["sign"] = sign_parameters(
        payload,
        client_secret=SYNTHETIC_SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )
    envelope = parse_push_envelope(_raw(payload))
    assert verify_push_envelope_signature(
        envelope,
        client_secret=SYNTHETIC_SECRET,
    )
    assert not verify_push_envelope_signature(
        envelope,
        client_secret="different-synthetic-secret",
    )


def test_synthetic_push_signature_golden_is_deterministic() -> None:
    assert sign_parameters(
        SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    ) == SYNTHETIC_PUSH_GOLDEN_SIGNATURE
    payload = {
        **SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        "sign": SYNTHETIC_PUSH_GOLDEN_SIGNATURE,
    }
    assert verify_push_envelope_signature(
        parse_push_envelope(_raw(payload)),
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
    )


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("device_name", "SYNTHETIC-PUSH-DEVICF"),
        (
            "data",
            '{"HeartRate":71,"BreathRate":16,"BodyShake":1,"OnBed":1,'
            '"ReportTime":"1767225600000",'
            '"DateTime":"2026-01-01T00:00:00.000"}',
        ),
        ("message_id", "synthetic-push-message-0002"),
        ("timestamp", 1767225601),
    ],
)
def test_synthetic_push_golden_rejects_semantic_changes(
    field: str,
    changed_value: object,
) -> None:
    payload = {
        **SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        field: changed_value,
        "sign": SYNTHETIC_PUSH_GOLDEN_SIGNATURE,
    }
    assert not verify_push_envelope_signature(
        parse_push_envelope(_raw(payload)),
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
    )


def test_push_verifier_rejects_invalid_or_missing_signature() -> None:
    invalid = {
        **SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        "sign": "AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
    assert not verify_push_envelope_signature(
        parse_push_envelope(_raw(invalid)),
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
    )
    with pytest.raises(PushContractError, match="sign"):
        parse_push_envelope(_raw(SYNTHETIC_PUSH_GOLDEN_PARAMETERS))


@pytest.mark.parametrize(
    ("signing_path", "key_mode"),
    [
        (PUSH_SIGNING_PATH, SigningKeyMode.DOCUMENTED_CLIENT_SECRET),
        (PLATFORM_SIGNING_PATH, SigningKeyMode.DOCUMENTED_CLIENT_SECRET),
        (PLATFORM_SIGNING_PATH, SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND),
    ],
)
def test_production_push_verifier_does_not_accept_diagnostic_alternatives(
    signing_path: str,
    key_mode: SigningKeyMode,
) -> None:
    alternative_signature = sign_parameters(
        SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
        signing_path=signing_path,
        key_mode=key_mode,
    )
    payload = {
        **SYNTHETIC_PUSH_GOLDEN_PARAMETERS,
        "sign": alternative_signature,
    }
    assert not verify_push_envelope_signature(
        parse_push_envelope(_raw(payload)),
        client_secret=SYNTHETIC_PUSH_GOLDEN_SECRET,
    )
    assert "key_mode" not in inspect.signature(
        verify_push_envelope_signature
    ).parameters


def test_documented_object_data_signing_is_explicitly_unresolved() -> None:
    envelope = parse_push_envelope(_fixture("vital_signs_data_event.json"))
    assert envelope.data_representation == DataRepresentation.OBJECT_DOCUMENTED_EXAMPLE
    with pytest.raises(SigningRepresentationUnresolved):
        verify_push_envelope_signature(
            envelope,
            client_secret=SYNTHETIC_SECRET,
        )


def test_valid_vital_normalizes_to_current_provider_neutral_candidates() -> None:
    candidates = _normalize(_fixture("vital_signs_data_event.json"))
    assert [item.observation_type for item in candidates] == [
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.MOVEMENT,
        ObservationType.BED_PRESENCE,
    ]
    assert isinstance(candidates[0].payload, HeartRatePayload)
    assert candidates[0].payload.value == 80
    assert isinstance(candidates[1].payload, RespiratoryRatePayload)
    assert candidates[1].payload.value == 18
    assert isinstance(candidates[2].payload, MovementPayload)
    assert candidates[2].payload.value == 20
    assert isinstance(candidates[3].payload, BedPresencePayload)
    assert candidates[3].payload.state == BedPresenceState.IN_BED
    for item in candidates:
        assert item.measurement_at is not None
        assert item.measurement_at.tzinfo == timezone.utc
        assert int(item.measurement_at.timestamp() * 1000) == 1772073482794
        assert item.source_timestamp_text == "1772073482794"
        assert item.timezone_status == TimezoneStatus.KNOWN


def test_report_time_is_always_interpreted_as_documented_unix_milliseconds() -> None:
    candidate = _normalize(_vital_raw(ReportTime=1000))[0]
    assert candidate.measurement_at == datetime(
        1970,
        1,
        1,
        0,
        0,
        1,
        tzinfo=timezone.utc,
    )
    assert candidate.source_timestamp_text == "1000"
    assert candidate.timezone_status == TimezoneStatus.KNOWN


@pytest.mark.parametrize(
    ("onbed", "expected"),
    [
        (0, BedPresenceState.OUT_OF_BED),
        (1, BedPresenceState.IN_BED),
        (2, BedPresenceState.UNKNOWN),
    ],
)
def test_push_onbed_codec_is_source_specific(
    onbed: int,
    expected: BedPresenceState,
) -> None:
    candidate = _normalize(_vital_raw(Onbed=onbed))[-1]
    assert isinstance(candidate.payload, BedPresencePayload)
    assert candidate.payload.state == expected
    if expected == BedPresenceState.UNKNOWN:
        assert candidate.quality.missing_state == MissingState.UNKNOWN
        assert "push_Onbed_unknown_value" in candidate.quality.quality_flags


def test_missing_and_vendor_invalid_vital_are_explicit_missing_candidates() -> None:
    payload = _payload()
    data = dict(payload["data"])
    data.pop("HeartRate")
    data["BreathRate"] = -1
    payload["data"] = data
    candidates = _normalize(_raw(payload))
    heart, respiratory = candidates[:2]
    assert isinstance(heart.payload, MissingIntervalPayload)
    assert heart.payload.target_observation_type == ObservationType.HEART_RATE
    assert heart.payload.missing_state == MissingState.MISSING
    assert isinstance(respiratory.payload, MissingIntervalPayload)
    assert respiratory.payload.target_observation_type == ObservationType.RESPIRATORY_RATE
    assert respiratory.payload.missing_state == MissingState.INVALID
    assert respiratory.payload.reason_code == "BreathRate_invalid_minus_one"


def test_alarm_event_parses_case_sensitive_fields_without_clinical_inference() -> None:
    envelope = parse_push_envelope(_fixture("alarm_event.json"))
    assert isinstance(envelope.event, AlarmEvent)
    assert envelope.event.alarm_id.value == "DOCUMENTED-ALARM-PLACEHOLDER"
    assert envelope.event.alarm_level.value == 2
    assert envelope.event.value.value == "107"
    assert envelope.event.alarm_reason.value == "documented reason placeholder"
    assert envelope.event.alarm_timestamp.value == "2024-08-09T16:54:00.002"
    assert envelope.event.alarm_params.state == FieldState.EMPTY
    assert envelope.event.firmware_version.value == "C.4.2.10"
    assert envelope.event.algorithm_version.value == "1.5.0"
    candidate = _normalize(_fixture("alarm_event.json"))[0]
    assert isinstance(candidate.payload, VendorAlertPayload)
    assert candidate.payload.alert_code == "107"
    assert candidate.payload.severity == AlertSeverity.UNKNOWN
    assert candidate.payload.lifecycle_state == AlertLifecycleState.ACTIVE
    assert candidate.event_occurred_at is None
    assert candidate.timezone_status == TimezoneStatus.TIMEZONE_UNKNOWN
    assert candidate.source_timestamp_text == "2024-08-09T16:54:00.002"
    assert "no_clinical_urgency_inferred" in candidate.quality.limitations


def test_alarm_stop_event_preserves_lifecycle_identity_and_timestamp_text() -> None:
    envelope = parse_push_envelope(_fixture("alarm_stop_event.json"))
    assert isinstance(envelope.event, AlarmStopEvent)
    assert envelope.event.alarm_id.value == "DOCUMENTED-ALARM-PLACEHOLDER"
    assert envelope.event.alarm_level.value == 1
    assert envelope.event.value.value == "107"
    assert envelope.event.stop_mode.value == "1"
    assert envelope.event.stop_timestamp.value == "2024-08-09T16:55:00.002"
    candidate = _normalize(_fixture("alarm_stop_event.json"))[0]
    assert isinstance(candidate.payload, VendorAlertPayload)
    assert candidate.payload.alert_code == "107"
    assert candidate.payload.lifecycle_state == AlertLifecycleState.STOPPED
    assert candidate.payload.vendor_alert_instance_id == (
        "DOCUMENTED-ALARM-PLACEHOLDER"
    )


@pytest.mark.parametrize(
    ("fixture_name", "state"),
    [
        ("connected_event.json", DeviceConnectivityState.ONLINE),
        ("disconnected_event.json", DeviceConnectivityState.OFFLINE),
    ],
)
def test_connectivity_events_use_current_provider_neutral_contract(
    fixture_name: str,
    state: DeviceConnectivityState,
) -> None:
    candidate = _normalize(_fixture(fixture_name))[0]
    assert isinstance(candidate.payload, DeviceConnectivityPayload)
    assert candidate.payload.state == state
    assert candidate.payload.vendor_status_code in {
        "ConnectedEvent",
        "DisconnectedEvent",
    }
    assert candidate.event_occurred_at is None
    assert candidate.timezone_status == TimezoneStatus.TIMEZONE_UNKNOWN


def test_same_raw_payload_replays_to_same_identity() -> None:
    raw = _fixture("vital_signs_data_event.json")
    first_envelope = parse_push_envelope(raw)
    second_envelope = parse_push_envelope(raw)
    assert first_envelope.idempotency_identity == second_envelope.idempotency_identity
    assert _normalize(raw) == _normalize(raw)


def test_changed_payload_with_same_message_id_is_collision_distinguishable() -> None:
    first_raw = _vital_raw(HeartRate=80)
    second_raw = _vital_raw(HeartRate=81)
    first_envelope = parse_push_envelope(first_raw)
    second_envelope = parse_push_envelope(second_raw)
    assert first_envelope.idempotency_identity == second_envelope.idempotency_identity
    assert first_envelope.raw_payload_sha256 != second_envelope.raw_payload_sha256
    first = _normalize(first_raw)[0]
    second = _normalize(second_raw)[0]
    assert first.idempotency_key == second.idempotency_key
    assert first.candidate_id != second.candidate_id
    assert first.provenance.raw_payload_sha256 != second.provenance.raw_payload_sha256


def test_logical_idempotency_is_scoped_to_provider_account() -> None:
    envelope = parse_push_envelope(_fixture("vital_signs_data_event.json"))
    first = normalize_push_envelope(
        envelope,
        provider_account_id="synthetic-account-one",
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )[0]
    second = normalize_push_envelope(
        envelope,
        provider_account_id="synthetic-account-two",
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED_AT,
    )[0]
    assert first.idempotency_key != second.idempotency_key


def test_alarm_id_and_raw_hash_are_deterministic_idempotency_fallbacks() -> None:
    alarm = _payload("alarm_event.json")
    alarm.pop("message_id")
    alarm_envelope = parse_push_envelope(_raw(alarm))
    assert alarm_envelope.idempotency_identity.startswith("provider-alarm-id.v1:")
    vital = _payload()
    vital.pop("message_id")
    vital_envelope = parse_push_envelope(_raw(vital))
    assert vital_envelope.idempotency_identity == (
        f"raw-payload-sha256.v1:{vital_envelope.raw_payload_sha256}"
    )


def test_candidate_provenance_is_complete_and_contains_no_raw_payload() -> None:
    raw = _fixture("vital_signs_data_event.json")
    envelope = parse_push_envelope(raw)
    candidate = _normalize(raw)[0]
    assert candidate.provider_id == "perceptor"
    assert candidate.provider_account_id == ACCOUNT_ID
    assert candidate.provider_device.provider_device_id == "1000000000000000002"
    assert candidate.provider_device.provider_device_name == (
        "DOCUMENTED-DEVICE-PLACEHOLDER"
    )
    assert candidate.provider_device.product_id == "1000000000000000001"
    assert candidate.provider_device.home_id == "1000000000000000003"
    assert candidate.provenance.adapter_id == ADAPTER_ID
    assert candidate.provenance.adapter_version == ADAPTER_VERSION
    assert candidate.provenance.raw_payload_sha256 == envelope.raw_payload_sha256
    assert candidate.provenance.source_record_id == (
        "documented-message-placeholder-vital"
    )
    assert candidate.received_at == RECEIVED_AT
    candidate_json = json.dumps(candidate.model_dump(mode="json"), sort_keys=True)
    assert "raw_body" not in candidate_json
    assert "HeartRate" not in candidate_json
    assert raw not in candidate_json.encode("utf-8")
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAA=" not in repr(envelope)


def test_naive_received_at_is_rejected_by_offline_boundary() -> None:
    envelope = parse_push_envelope(_fixture("vital_signs_data_event.json"))
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_push_envelope(
            envelope,
            provider_account_id=ACCOUNT_ID,
            data_mode=DataMode.REPLAY,
            received_at=datetime(2026, 8, 20, 12, 0),
        )
