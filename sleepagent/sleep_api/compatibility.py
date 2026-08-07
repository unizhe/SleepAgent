"""Compatibility policy and response headers for public API version 1."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Response


API_VERSION = "v1"
API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class CompatibilityPolicy:
    api_version: str = API_VERSION
    additive_fields_are_compatible: bool = True
    unknown_request_fields_are_rejected: bool = True
    breaking_changes_require_new_version: bool = True
    deprecation_requires_overlap_window: bool = True


V1_COMPATIBILITY_POLICY = CompatibilityPolicy()


def apply_version_headers(response: Response) -> None:
    response.headers["X-API-Version"] = API_VERSION
    response.headers["Deprecation"] = "false"


__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "CompatibilityPolicy",
    "V1_COMPATIBILITY_POLICY",
    "apply_version_headers",
]
