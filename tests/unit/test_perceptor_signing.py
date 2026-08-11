import base64
import hashlib
import hmac

from sleepagent.integrations.perceptor.signing import (
    build_canonical_query_string,
    build_string_to_sign,
    percent_encode,
    sign_parameters,
)


def test_percent_encode_matches_perceptor_prototype_rules() -> None:
    assert percent_encode("/v2") == "%2Fv2"
    assert percent_encode("two words") == "two%20words"
    assert percent_encode("a/b*c~") == "a%2Fb%2Ac~"


def test_canonical_query_sorts_keys_and_excludes_sign_empty_values() -> None:
    params = {
        "sign": "ignore-me",
        "device_name": "imei/001",
        "empty": "",
        "none": None,
        "client_id": "client-id",
        "timestamp": 1_700_000_000,
        "sign_nonce": "nonce with space",
    }

    assert build_canonical_query_string(params) == (
        "client_id=client-id&"
        "device_name=imei%2F001&"
        "sign_nonce=nonce%20with%20space&"
        "timestamp=1700000000"
    )


def test_sign_parameters_uses_post_v2_string_and_secret_ampersand() -> None:
    params = {
        "client_id": "client-id",
        "version": "2.0",
        "timestamp": "1700000000",
        "sign_version": "2.0",
        "sign_nonce": "nonce-fixed",
        "sign_method": "HMAC-SHA1",
        "device_name": "imei-001",
        "home_id": 123,
    }

    string_to_sign = build_string_to_sign(params)
    expected_string = (
        "POST&%2Fv2&"
        "client_id%3Dclient-id%26"
        "device_name%3Dimei-001%26"
        "home_id%3D123%26"
        "sign_method%3DHMAC-SHA1%26"
        "sign_nonce%3Dnonce-fixed%26"
        "sign_version%3D2.0%26"
        "timestamp%3D1700000000%26"
        "version%3D2.0"
    )
    assert string_to_sign == expected_string

    expected_signature = base64.b64encode(
        hmac.new(
            b"secret&",
            expected_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    assert (
        sign_parameters(params, client_secret="secret", append_ampersand=True)
        == expected_signature
    )
    assert sign_parameters(
        params,
        client_secret="secret",
        append_ampersand=False,
    ) != expected_signature
