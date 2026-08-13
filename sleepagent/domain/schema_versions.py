# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Fail-closed dispatch for persisted versioned JSON contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar


T = TypeVar("T")


class VersionDispatchError(ValueError):
    """Persisted JSON is missing, unknown, or invalid for its declared version."""


def dispatch_versioned(
    payload: Mapping[str, Any],
    *,
    family: str,
    readers: Mapping[str, Callable[[Mapping[str, Any]], T]],
) -> T:
    version = payload.get("schema_version")
    if not isinstance(version, str) or not version:
        raise VersionDispatchError(f"{family} schema_version is required")
    reader = readers.get(version)
    if reader is None:
        raise VersionDispatchError(
            f"unsupported {family} schema_version: {version!r}"
        )
    try:
        return reader(payload)
    except VersionDispatchError:
        raise
    except Exception as exc:
        raise VersionDispatchError(
            f"invalid {family} payload for {version}"
        ) from exc


def dispatch_versioned_json(
    raw: str | bytes | bytearray,
    *,
    family: str,
    readers: Mapping[str, Callable[[Mapping[str, Any]], T]],
) -> T:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionDispatchError(f"{family} payload is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise VersionDispatchError(f"{family} payload must be a JSON object")
    return dispatch_versioned(value, family=family, readers=readers)


__all__ = [
    "VersionDispatchError",
    "dispatch_versioned",
    "dispatch_versioned_json",
]
