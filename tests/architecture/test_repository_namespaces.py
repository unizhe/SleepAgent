from __future__ import annotations

import importlib
from pathlib import Path

import sleepagent.product_runtime as product_runtime
from sleepagent.product_api.diagnostics import RADAR_AGENT_API_PREFIX, full_route_paths
from sleepagent.product_device.provider import ReplayRadarProvider
from sleepagent.product_runtime.cli import build_demo_payload, build_parser
from sleepagent.product_runtime.schemas import EvidenceClaim, ReviewStatus


ROOT = Path(__file__).resolve().parents[2]


def test_responsibility_aligned_namespaces_are_importable() -> None:
    for namespace in (
        "sleepagent.product_runtime",
        "sleepagent.product_runtime.agents",
        "sleepagent.product_runtime.tools",
        "sleepagent.product_runtime.services",
        "sleepagent.product_runtime.policies",
        "sleepagent.product_api",
        "sleepagent.product_device",
        "sleepagent.persistence",
        "sleepagent.sleep_domain",
        "sleepagent.sleep_api",
        "sleepagent.integrations.perceptor",
        "sleepagent.simulation.replay",
    ):
        importlib.import_module(namespace)

    assert len(product_runtime.__all__) == 25


def test_replay_provider_satisfies_device_provider_contract_shape() -> None:
    provider = ReplayRadarProvider()
    devices = provider.list_devices()
    health = provider.health_check()

    assert devices
    assert provider.get_device(devices[0].radar_device_id).radar_device_id
    assert provider.pull_snapshots(devices[0].radar_device_id)
    assert provider.pull_night_report(devices[0].radar_device_id) is not None
    assert provider.receive_webhook({"message_id": "demo-event"}).accepted is True
    assert health.healthy is True
    assert health.mode == "fake_replay"


def test_reviewed_evidence_claim_requires_evidence_refs() -> None:
    import pytest

    with pytest.raises(ValueError, match="evidence_refs"):
        EvidenceClaim(
            claim_id="claim-without-evidence",
            task_id="task-demo",
            text="Reviewed claims must not be unsupported.",
            generated_by="test",
            review_status=ReviewStatus.REVIEWED,
        )


def test_diagnostic_api_and_cli_contracts_remain_stable() -> None:
    assert RADAR_AGENT_API_PREFIX == "/radar-agent"
    assert "/radar-agent/tasks" in full_route_paths()

    parser = build_parser()
    args = parser.parse_args(
        ["run-demo", "--scenario", "frequent_out_of_bed", "--format", "jsonl"]
    )
    assert args.command == "run-demo"
    payload = build_demo_payload("frequent_out_of_bed")
    assert payload["expected"]["risk_level"] == "watch"  # type: ignore[index]


def test_historical_namespace_is_physically_absent() -> None:
    assert not (ROOT / "sleepagent" / "radar_agent").exists()
    violations = [
        path.relative_to(ROOT)
        for root in (ROOT / "sleepagent", ROOT / "backend", ROOT / "reference_client")
        for path in root.rglob("*.py")
        if "sleepagent.radar_agent" in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_frontend_entrypoint_keeps_radar_primary() -> None:
    home_page = (ROOT / "frontend/app/page.tsx").read_text(encoding="utf-8")
    assert "RadarWorkspace" in home_page
    assert "LegacySleepWorkspace" not in home_page
