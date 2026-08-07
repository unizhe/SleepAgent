from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote


DEFAULT_SIGNING_PATH = "/v2"
DEFAULT_SIGNATURE_METHOD = "HMAC-SHA1"


def percent_encode(value: Any) -> str:
    encoded = quote(str(value), safe="")
    return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def build_canonical_query_string(params: Mapping[str, Any]) -> str:
    items: list[str] = []
    sorted_keys = sorted(
        (item_key for item_key in params if str(item_key) != "sign"),
        key=lambda item_key: str(item_key),
    )
    for key in sorted_keys:
        value = params[key]
        if value is None or value == "":
            continue
        items.append(f"{percent_encode(str(key))}={percent_encode(value)}")
    return "&".join(items)


def build_string_to_sign(
    params: Mapping[str, Any],
    *,
    http_method: str = "POST",
    signing_path: str = DEFAULT_SIGNING_PATH,
) -> str:
    canonical_query = build_canonical_query_string(params)
    return (
        f"{http_method.upper()}&"
        f"{percent_encode(signing_path)}&"
        f"{percent_encode(canonical_query)}"
    )


def sign_parameters(
    params: Mapping[str, Any],
    *,
    client_secret: str,
    append_ampersand: bool = True,
    http_method: str = "POST",
    signing_path: str = DEFAULT_SIGNING_PATH,
) -> str:
    if not client_secret:
        raise ValueError("client_secret is required for Perceptor signing.")
    key = f"{client_secret}&" if append_ampersand else client_secret
    message = build_string_to_sign(
        params,
        http_method=http_method,
        signing_path=signing_path,
    )
    digest = hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")
