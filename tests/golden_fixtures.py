from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


_FIXTURE_ROOT = Path(__file__).with_name("fixtures")


def load_phase3a_capability_goldens() -> dict[str, Any]:
    return json.loads(
        (_FIXTURE_ROOT / "phase3a_capability_goldens.json").read_text(
            encoding="utf-8"
        )
    )


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["canonical_json_sha256", "load_phase3a_capability_goldens"]
