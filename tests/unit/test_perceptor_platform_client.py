from __future__ import annotations

import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from sleepagent.integrations.perceptor.client import (
    DEVICE_DETAIL_ENDPOINT,
    DEVICE_LIST_ENDPOINT,
    GET_CURRENT_ENDPOINT,
    HISTORY_ENDPOINT,
    PRODUCT_LIST_ENDPOINT,
    REALTIME_READ_ENDPOINT,
    REALTIME_START_ENDPOINT,
    SLEEP_REPORT_ENDPOINT,
    TOKEN_ENDPOINT,
    PlatformApiError,
    PlatformEvidencedRead,
    PlatformRawResponseEvidence,
    PerceptorPlatformClient,
)
from sleepagent.integrations.perceptor.push import verify_push_envelope_signature
from sleepagent.integrations.perceptor.signing import (
    PLATFORM_SIGNING_KEY_MODE,
    PLATFORM_SIGNING_PATH,
    PUSH_SIGNING_KEY_MODE,
    PUSH_SIGNING_PATH,
    SigningKeyMode,
    sign_parameters,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
CLIENT_ID = "synthetic-platform-client"
CLIENT_SECRET = "synthetic-platform-secret"
ACCESS_TOKEN = "synthetic-access-token-never-real"


def _response(data: object, *, code: str = "200", success: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "request_id": "synthetic-request",
            "success": success,
            "code": code,
            "message": "synthetic",
            "timestamp": "1787472000",
            "data": data,
        },
    )


def _client(
    handler: Any,
    *,
    now_factory: Any = lambda: NOW,
) -> tuple[PerceptorPlatformClient, httpx.Client]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PerceptorPlatformClient(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        http_client=http,
        now_factory=now_factory,
        nonce_factory=lambda: "synthetic-nonce-0001",
    )
    return client, http


def test_platform_ampersand_v2_synthetic_golden_is_fixed() -> None:
    params = {
        "client_id": "contract-client",
        "device_name": "设备 A/B",
        "empty": "",
        "sign": "excluded",
        "timestamp": 1723193640,
        "version": "2.0",
    }
    assert PLATFORM_SIGNING_PATH == "/v2"
    assert PLATFORM_SIGNING_KEY_MODE == SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND
    assert sign_parameters(
        params,
        client_secret="synthetic-contract-secret",
        signing_path=PLATFORM_SIGNING_PATH,
        key_mode=PLATFORM_SIGNING_KEY_MODE,
    ) == "Z/Zqfj+SsUTNBd8al5+o9hd0JIY="


def test_product_read_uses_one_frozen_signature_and_bearer_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token/get"):
            return _response(
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": None,
                    "expires_time": 1,
                }
            )
        assert request.url.path.endswith(PRODUCT_LIST_ENDPOINT)
        return _response(
            {
                "list": [
                    {
                        "product_id": 7000000000000000001,
                        "product_name": "Synthetic Radar",
                        "product_model": "SYNTHETIC-Z1",
                        "device_amount": 1,
                    }
                ]
            }
        )

    client, http = _client(handler)
    try:
        products = client.product_list()
    finally:
        http.close()
    assert len(products) == 1
    assert products[0].product_id == "7000000000000000001"
    assert len(requests) == 2
    signed = json.loads(requests[1].content)
    signature = signed.pop("sign")
    expected = sign_parameters(
        signed,
        client_secret=CLIENT_SECRET,
        signing_path=PLATFORM_SIGNING_PATH,
        key_mode=SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND,
    )
    documented = sign_parameters(
        signed,
        client_secret=CLIENT_SECRET,
        signing_path=PLATFORM_SIGNING_PATH,
        key_mode=SigningKeyMode.DOCUMENTED_CLIENT_SECRET,
    )
    assert signature == expected
    assert signature != documented
    assert requests[1].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert "key_mode" not in inspect.signature(client.product_list).parameters
    assert "key_mode" not in inspect.signature(client.device_list).parameters
    assert "key_mode" not in inspect.signature(client.device_detail).parameters


def test_token_cache_uses_observed_expiry_selector_and_refreshes_once() -> None:
    clock = [NOW]
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/token/get"):
            token_requests += 1
            return _response(
                {
                    "access_token": f"{ACCESS_TOKEN}-{token_requests}",
                    "token_type": "Bearer",
                    "expires_in": None,
                    "expires_time": 1,
                }
            )
        return _response({"list": []})

    client, http = _client(handler, now_factory=lambda: clock[0])
    try:
        first = client.token()
        second = client.token()
        clock[0] = NOW + timedelta(days=1)
        third = client.token()
    finally:
        http.close()
    assert first is second
    assert first.expiry_selector == 1
    assert first.expires_at == NOW + timedelta(days=1)
    assert third is not first
    assert token_requests == 2
    assert ACCESS_TOKEN not in repr(first)


def test_concurrent_token_requests_are_single_flight() -> None:
    token_requests = 0
    count_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        assert request.url.path.endswith("/token/get")
        with count_lock:
            token_requests += 1
        time.sleep(0.05)
        return _response(
            {
                "access_token": ACCESS_TOKEN,
                "token_type": "Bearer",
                "expires_time": 1,
            }
        )

    client, http = _client(handler)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            leases = tuple(executor.map(lambda _index: client.token(), range(4)))
    finally:
        http.close()
    assert all(lease is leases[0] for lease in leases)
    assert token_requests == 1


def test_device_list_and_detail_map_provider_neutral_identity() -> None:
    requests: list[httpx.Request] = []
    record = {
        "device_id": 7000000000000000011,
        "device_name": "SYNTHETIC-DEVICE-001",
        "product_id": 7000000000000000012,
        "home_id": 7000000000000000013,
        "project_id": 7000000000000000014,
        "device_status": "ONLINE",
        "firmware_version": "synthetic-fw",
        "hardware_version": "synthetic-hw",
        "device_imei": "SYNTHETIC-IMEI",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token/get"):
            return _response(
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_time": 1,
                }
            )
        if request.url.path.endswith(DEVICE_LIST_ENDPOINT):
            return _response(
                {
                    "page_count": 1,
                    "page_size": 50,
                    "current_page": 1,
                    "total": 1,
                    "total_records": 1,
                    "list": [record],
                }
            )
        if request.url.path.endswith(DEVICE_DETAIL_ENDPOINT):
            return _response(record)
        raise AssertionError("unexpected endpoint")

    client, http = _client(handler)
    try:
        page = client.device_page()
        listed = page.devices
        detail = client.device_detail(device_name=listed[0].device_name)
    finally:
        http.close()
    assert listed == (detail,)
    assert page.page_count == 1
    assert page.total_records == 1
    identity = detail.provider_identity()
    assert identity.provider_device_id == "7000000000000000011"
    assert identity.provider_device_name == "SYNTHETIC-DEVICE-001"
    assert identity.product_id == "7000000000000000012"
    assert identity.home_id == "7000000000000000013"
    assert identity.native_keys == {"device_imei": "SYNTHETIC-IMEI"}
    detail_payload = json.loads(requests[-1].content)
    assert detail_payload["device_name"] == "SYNTHETIC-DEVICE-001"
    assert "home_id" not in detail_payload


def test_vendor_error_is_redacted_and_never_auto_falls_back() -> None:
    signed_requests = 0
    leaked = "must-not-appear-secret-token-signature"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal signed_requests
        if request.url.path.endswith("/token/get"):
            return _response(
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_time": 1,
                }
            )
        signed_requests += 1
        return _response(
            {}, code="1003", success=False
        )

    client, http = _client(handler)
    try:
        with pytest.raises(PlatformApiError) as caught:
            client.product_list()
    finally:
        http.close()
    assert caught.value.category == "SIGNATURE_ERROR"
    assert caught.value.vendor_code == "1003"
    assert signed_requests == 1
    assert CLIENT_SECRET not in str(caught.value)
    assert ACCESS_TOKEN not in str(caught.value)
    assert leaked not in str(caught.value)


def test_pull_methods_use_documented_request_shapes_and_capture_exact_bytes() -> None:
    requests: list[httpx.Request] = []
    evidence: list[PlatformRawResponseEvidence] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token/get"):
            return _response({"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_time": 1})
        if request.url.path.endswith(HISTORY_ENDPOINT):
            return _response([{"device_id": "SYNTHETIC-DEVICE"}])
        if request.url.path.endswith(REALTIME_START_ENDPOINT):
            return _response({"end_time": 1787472180})
        return _response({"synthetic": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PerceptorPlatformClient(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, http_client=http,
        now_factory=lambda: NOW, nonce_factory=lambda: "synthetic-nonce-0001",
        response_observer=evidence.append,
    )
    try:
        client.get_current(device_name="SYNTHETIC-DEVICE", home_id="7000000000000000001")
        client.start_realtime(device_name="SYNTHETIC-DEVICE", home_id="7000000000000000001")
        client.get_realtime(device_name="SYNTHETIC-DEVICE", home_id="7000000000000000001")
        client.get_history(device_names=("SYNTHETIC-DEVICE",), start_at=NOW, end_at=NOW + timedelta(minutes=5))
        client.get_sleep_report(device_name="SYNTHETIC-DEVICE", home_id="7000000000000000001", report_date=NOW.date())
    finally:
        http.close()

    endpoint_requests = {request.url.path.rsplit("/v2", 1)[-1]: request for request in requests[1:]}
    assert set(endpoint_requests) == {
        GET_CURRENT_ENDPOINT, REALTIME_START_ENDPOINT, REALTIME_READ_ENDPOINT,
        HISTORY_ENDPOINT, SLEEP_REPORT_ENDPOINT,
    }
    history = json.loads(endpoint_requests[HISTORY_ENDPOINT].content)
    assert history["devices"] == ["SYNTHETIC-DEVICE"]
    assert history["start_time"] == int(NOW.timestamp())
    assert history["end_time"] == int((NOW + timedelta(minutes=5)).timestamp())
    signing_copy = dict(history)
    signature = signing_copy.pop("sign")
    signing_copy["devices"] = '["SYNTHETIC-DEVICE"]'
    assert signature == sign_parameters(
        signing_copy, client_secret=CLIENT_SECRET,
        signing_path=PLATFORM_SIGNING_PATH, key_mode=PLATFORM_SIGNING_KEY_MODE,
    )
    assert len(evidence) == 6
    assert all(item.raw_body and len(item.raw_sha256) == 64 for item in evidence)
    assert ACCESS_TOKEN not in repr(evidence)


def test_evidenced_pull_reads_atomically_pair_parsed_data_with_exact_raw_bytes() -> None:
    observed: list[PlatformRawResponseEvidence] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_ENDPOINT):
            return _response(
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_time": 1,
                }
            )
        if request.url.path.endswith(GET_CURRENT_ENDPOINT):
            return _response({"surface": "current"})
        if request.url.path.endswith(REALTIME_START_ENDPOINT):
            return _response({"surface": "start"})
        if request.url.path.endswith(REALTIME_READ_ENDPOINT):
            return _response({"surface": "realtime"})
        if request.url.path.endswith(HISTORY_ENDPOINT):
            return _response([{"surface": "history"}])
        if request.url.path.endswith(SLEEP_REPORT_ENDPOINT):
            return _response({"surface": "sleep-report"})
        raise AssertionError("unexpected endpoint")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PerceptorPlatformClient(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        http_client=http,
        now_factory=lambda: NOW,
        nonce_factory=lambda: "synthetic-nonce-0001",
        response_observer=observed.append,
    )
    try:
        reads = (
            client.get_current_evidenced(
                device_name="SYNTHETIC-DEVICE",
                home_id="7000000000000000001",
            ),
            client.start_realtime_evidenced(
                device_name="SYNTHETIC-DEVICE",
                home_id="7000000000000000001",
            ),
            client.get_realtime_evidenced(
                device_name="SYNTHETIC-DEVICE",
                home_id="7000000000000000001",
            ),
            client.get_history_evidenced(
                device_names=("SYNTHETIC-DEVICE",),
                start_at=NOW,
                end_at=NOW + timedelta(minutes=5),
            ),
            client.get_sleep_report_evidenced(
                device_name="SYNTHETIC-DEVICE",
                home_id="7000000000000000001",
                report_date=NOW.date(),
            ),
        )
    finally:
        http.close()

    assert tuple(read.endpoint for read in reads) == (
        GET_CURRENT_ENDPOINT,
        REALTIME_START_ENDPOINT,
        REALTIME_READ_ENDPOINT,
        HISTORY_ENDPOINT,
        SLEEP_REPORT_ENDPOINT,
    )
    for read in reads:
        assert read.endpoint == read.evidence.endpoint
        parsed_raw_data = json.loads(read.evidence.raw_body)["data"]
        expected_data = list(read.data) if read.endpoint == HISTORY_ENDPOINT else read.data
        assert parsed_raw_data == expected_data
    assert [item.endpoint for item in observed] == [
        TOKEN_ENDPOINT,
        GET_CURRENT_ENDPOINT,
        REALTIME_START_ENDPOINT,
        REALTIME_READ_ENDPOINT,
        HISTORY_ENDPOINT,
        SLEEP_REPORT_ENDPOINT,
    ]
    with pytest.raises(FrozenInstanceError):
        reads[0].endpoint = HISTORY_ENDPOINT  # type: ignore[misc]


def test_token_evidence_cannot_be_constructed_as_a_data_read() -> None:
    token_evidence = PlatformRawResponseEvidence(
        endpoint=TOKEN_ENDPOINT,
        requested_at=NOW,
        received_at=NOW,
        http_status=200,
        raw_body=b'{"data":{"access_token":"synthetic"}}',
    )
    with pytest.raises(ValueError, match="token response"):
        PlatformEvidencedRead(
            endpoint=TOKEN_ENDPOINT,
            data={"access_token": "synthetic"},
            evidence=token_evidence,
        )
    with pytest.raises(ValueError, match="does not match"):
        PlatformEvidencedRead(
            endpoint=GET_CURRENT_ENDPOINT,
            data={},
            evidence=token_evidence,
        )


def test_history_limits_and_integer_home_id_fail_closed() -> None:
    client, http = _client(lambda _request: _response({}))
    try:
        with pytest.raises(ValueError, match="at most one hour"):
            client.get_history(device_names=("SYNTHETIC",), start_at=NOW, end_at=NOW + timedelta(hours=2))
        with pytest.raises(ValueError, match="integer representation"):
            client.get_current(device_name="SYNTHETIC", home_id="not-an-integer")
    finally:
        http.close()


def test_read_only_allowlist_and_push_contract_remain_independent() -> None:
    assert PRODUCT_LIST_ENDPOINT != PUSH_SIGNING_PATH
    assert PUSH_SIGNING_PATH == ""
    assert PUSH_SIGNING_KEY_MODE == SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND
    assert "key_mode" not in inspect.signature(
        verify_push_envelope_signature
    ).parameters


@pytest.mark.parametrize(
    ("page_current", "page_size"), [(0, 50), (1, 0), (1, 51)]
)
def test_device_list_bounds_fail_before_network(
    page_current: int, page_size: int
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not run")

    client, http = _client(handler)
    try:
        with pytest.raises(ValueError):
            client.device_list(page_current=page_current, page_size=page_size)
    finally:
        http.close()
