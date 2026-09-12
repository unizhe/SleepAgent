#!/usr/bin/env python3
"""Generate or verify deterministic OpenAPI snapshots from the canonical app."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sleepagent.app import create_sleep_backend_app
from sleepagent.process import RuntimeServices
from sleepagent.config import ApiSurface, DataMode, ProcessRole
from sleepagent.api.product import (
    FailClosedProductIdentityResolver,
    ProductApiService,
)

OUTPUTS = {
    "backend-bff-v1.json": frozenset(
        {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
    ),
    "demo-v1.json": frozenset({ApiSurface.DEMO}),
}


class _UnusedBackend:
    pass


class _UnusedDemoController:
    pass


@dataclass
class _SnapshotRuntime:
    surfaces: frozenset[ApiSurface]

    def __post_init__(self) -> None:
        self.settings = SimpleNamespace(
            process_role=ProcessRole.API,
            enabled_surfaces=self.surfaces,
            data_mode=DataMode.REPLAY,
            demo_controller_token=SimpleNamespace(
                get_secret_value=lambda: "snapshot-demo-token-32-bytes-minimum"
            ),
            max_compressed_body_bytes=1_048_576,
            max_decompressed_body_bytes=2_097_152,
            max_json_depth=32,
            max_json_members=20_000,
            request_timeout_seconds=15.0,
        )
        self.services = RuntimeServices(
            product=(
                ProductApiService(
                    identity_resolver=FailClosedProductIdentityResolver(),
                    backend=_UnusedBackend(),  # type: ignore[arg-type]
                )
                if ApiSurface.PRODUCT in self.surfaces
                else None
            ),
            demo=(
                _UnusedDemoController()
                if ApiSurface.DEMO in self.surfaces
                else None
            ),
            public_v1_runtime_provider=(
                (lambda: object())
                if ApiSurface.PUBLIC_V1 in self.surfaces
                else None
            ),
        )
        self.worker_handlers: dict[str, Any] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _canonical_snapshot(surfaces: frozenset[ApiSurface]) -> bytes:
    app = create_sleep_backend_app(
        _SnapshotRuntime(surfaces),  # type: ignore[arg-type]
    )
    return (
        json.dumps(
            app.openapi(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_root = ROOT / "docs" / "contracts" / "openapi"
    if not args.check:
        output_root.mkdir(parents=True, exist_ok=True)
    drifted: list[str] = []
    for filename, surfaces in OUTPUTS.items():
        expected = _canonical_snapshot(surfaces)
        path = output_root / filename
        if args.check:
            if not path.exists() or path.read_bytes() != expected:
                drifted.append(filename)
        else:
            path.write_bytes(expected)
    if drifted:
        raise SystemExit("OpenAPI snapshot drift: " + ", ".join(drifted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
