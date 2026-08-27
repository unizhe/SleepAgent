"""YunYun/Perceptor OpenAPI V2.5.2 signing primitives.

P4-C-R2 proved that incoming Push uses the empty signing path and
``ClientSecret + "&"``. P4-D2-A independently proved that this tenant's
Platform API uses the documented ``/v2`` path with the same ampersand key
delta. Push and Platform remain separate endpoint contracts, and no runtime
verifier or client automatically tries multiple constructions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
from collections.abc import Mapping
from enum import Enum
from typing import TypeAlias
from urllib.parse import quote


PLATFORM_SIGNING_PATH = "/v2"
PUSH_SIGNING_PATH = ""
PLATFORM_SIGNATURE_GOLDEN_STATUS = "VERIFIED_REAL_CONTRACT_DELTA"
PLATFORM_SIGNING_KEY_STATUS = PLATFORM_SIGNATURE_GOLDEN_STATUS
PUSH_SIGNATURE_GOLDEN_STATUS = "VERIFIED_REAL_CONTRACT_DELTA"

SigningScalar: TypeAlias = str | int | float


class SigningContractError(ValueError):
    """The supplied values cannot be represented by the frozen contract."""


class SigningRepresentationUnresolved(SigningContractError):
    """V2.5.2 does not define an executable representation for this value."""


class SigningKeyMode(str, Enum):
    DOCUMENTED_CLIENT_SECRET = "documented_client_secret"
    CLIENT_SECRET_PLUS_AMPERSAND = "client_secret_plus_ampersand"


PUSH_SIGNING_KEY_MODE = SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND
PLATFORM_SIGNING_KEY_MODE = SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND


def percent_encode(value: str) -> str:
    """Encode one UTF-8 string using the RFC3986 rules stated in V2.5.2."""

    if not isinstance(value, str):
        raise TypeError("percent_encode requires a string")
    return quote(value, safe="-_.~", encoding="utf-8", errors="strict")


def build_canonical_query_string(
    parameters: Mapping[str, SigningScalar | None | object],
) -> str:
    """Sort and encode parameters, excluding only ``sign``.

    Empty strings are retained as ``name=`` because the document instructs the
    caller to sign request parameters and does not authorize omission. Null and
    compound values fail closed until their representation is vendor-proven.
    """

    if any(not isinstance(key, str) for key in parameters):
        raise SigningContractError("signing parameter names must be strings")
    parts: list[str] = []
    for key in sorted(item for item in parameters if item != "sign"):
        value = parameters[key]
        text = _signing_text(value, name=key)
        parts.append(f"{percent_encode(key)}={percent_encode(text)}")
    return "&".join(parts)


def build_string_to_sign(
    parameters: Mapping[str, SigningScalar | None | object],
    *,
    signing_path: str = PLATFORM_SIGNING_PATH,
    http_method: str = "POST",
) -> str:
    if http_method.upper() != "POST":
        raise SigningContractError("V2.5.2 signing is frozen to POST")
    if signing_path not in {PLATFORM_SIGNING_PATH, PUSH_SIGNING_PATH}:
        raise SigningContractError("unsupported V2.5.2 signing path")
    canonical = build_canonical_query_string(parameters)
    return f"POST&{percent_encode(signing_path)}&{percent_encode(canonical)}"


def sign_parameters(
    parameters: Mapping[str, SigningScalar | None | object],
    *,
    client_secret: str,
    signing_path: str = PLATFORM_SIGNING_PATH,
    key_mode: SigningKeyMode = SigningKeyMode.DOCUMENTED_CLIENT_SECRET,
) -> str:
    if not isinstance(client_secret, str) or not client_secret:
        raise SigningContractError("a non-empty client secret is required")
    if key_mode == SigningKeyMode.DOCUMENTED_CLIENT_SECRET:
        key = client_secret
    elif key_mode == SigningKeyMode.CLIENT_SECRET_PLUS_AMPERSAND:
        key = f"{client_secret}&"
    else:
        raise SigningContractError("unsupported signing key mode")
    message = build_string_to_sign(parameters, signing_path=signing_path)
    digest = hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_parameters_signature(
    parameters: Mapping[str, SigningScalar | None | object],
    *,
    supplied_signature: str,
    client_secret: str,
    signing_path: str = PLATFORM_SIGNING_PATH,
    key_mode: SigningKeyMode = SigningKeyMode.DOCUMENTED_CLIENT_SECRET,
) -> bool:
    if not isinstance(supplied_signature, str):
        return False
    expected = sign_parameters(
        parameters,
        client_secret=client_secret,
        signing_path=signing_path,
        key_mode=key_mode,
    )
    return hmac.compare_digest(supplied_signature, expected)


def signature_sha256(signature: str) -> str:
    """Return a safe diagnostic fingerprint without exposing the signature."""

    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _signing_text(value: object, *, name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise SigningRepresentationUnresolved(
            f"boolean representation is unresolved for parameter {name!r}"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SigningContractError(
                f"non-finite number is forbidden for parameter {name!r}"
            )
        return str(value)
    if value is None:
        raise SigningRepresentationUnresolved(
            f"null representation is unresolved for parameter {name!r}"
        )
    raise SigningRepresentationUnresolved(
        f"compound representation is unresolved for parameter {name!r}"
    )


__all__ = [
    "PLATFORM_SIGNING_PATH",
    "PLATFORM_SIGNING_KEY_MODE",
    "PLATFORM_SIGNING_KEY_STATUS",
    "PLATFORM_SIGNATURE_GOLDEN_STATUS",
    "PUSH_SIGNING_PATH",
    "PUSH_SIGNING_KEY_MODE",
    "PUSH_SIGNATURE_GOLDEN_STATUS",
    "SigningContractError",
    "SigningKeyMode",
    "SigningRepresentationUnresolved",
    "build_canonical_query_string",
    "build_string_to_sign",
    "percent_encode",
    "sign_parameters",
    "signature_sha256",
    "verify_parameters_signature",
]
