from __future__ import annotations

import hashlib
from pathlib import Path

from tests.support.golden_fixtures import load_product_capability_goldens


FIXTURE = Path(__file__).parents[1] / "fixtures" / "product_capability_goldens.json"
FROZEN_FIXTURE_SHA256 = (
    "ac09f68e1334210d9e00d4491226fede856752d484b939d3683f2bc2635851ea"
)


def test_memory_routing_remains_a_frozen_behavior_fixture() -> None:
    expected = load_product_capability_goldens()["memory_routing"]

    assert expected == {
        "candidate_count": 3,
        "candidate_types": ["care_event", "preference", "trend"],
    }
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        FROZEN_FIXTURE_SHA256
    )
