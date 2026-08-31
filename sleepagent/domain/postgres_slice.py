"""Compatibility facade for the infrastructure-owned PostgreSQL adapter."""

from __future__ import annotations

import importlib


_authority = importlib.import_module(
    "sleepagent.infrastructure.postgres_sleep_slice"
)
__all__ = list(_authority.__all__)
globals().update({name: getattr(_authority, name) for name in __all__})
