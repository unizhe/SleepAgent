from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from sleepagent.radar_agent import (
    CANONICAL_SUBPACKAGES,
    RADAR_AGENT_API_PREFIX,
)
from sleepagent.radar_agent.a2a import InMemoryA2AMailbox
from sleepagent.radar_agent.api import full_route_paths
from sleepagent.radar_agent.cli import build_demo_payload, build_parser
from sleepagent.radar_agent.evidence import EvidenceLedgerBuilder
from sleepagent.radar_agent.orchestrator import WORKFLOW_NODE_ORDER, WorkflowNodeName
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    EvidenceClaim,
    ReviewStatus,
    RiskLevel,
)


def test_radar_agent_subpackages_are_importable() -> None:
    for subpackage in CANONICAL_SUBPACKAGES:
        importlib.import_module(f"sleepagent.radar_agent.{subpackage}")


def test_replay_provider_satisfies_radar_provider_contract_shape() -> None:
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
    with pytest.raises(ValueError, match="evidence_refs"):
        EvidenceClaim(
            claim_id="claim-without-evidence",
            task_id="task-demo",
            text="Reviewed claims must not be unsupported.",
            generated_by="test",
            review_status=ReviewStatus.REVIEWED,
        )


def test_evidence_ledger_and_a2a_mailbox_are_minimally_usable() -> None:
    claim = EvidenceClaim(
        claim_id="claim-1",
        task_id="task-demo",
        text="Latest replay night has a usable summary.",
        evidence_refs=["night-summary:demo"],
        confidence=0.82,
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    builder = EvidenceLedgerBuilder(ledger_id="ledger-demo", task_id="task-demo")
    builder.add_raw_ref("snapshot:demo")
    builder.add_canonical_ref("night-summary:demo")
    builder.add_metric("data_coverage_ratio", 0.91)
    builder.add_claim(claim)

    ledger = builder.build()
    mailbox = InMemoryA2AMailbox()
    message = mailbox.publish(
        A2AMessage(
            message_id="msg-1",
            sender="trend",
            receiver="risk_signal",
            task_id="task-demo",
            intent="review_trend_claim",
            evidence_refs=[claim.claim_id],
            confidence=0.8,
            risk_level=RiskLevel.WATCH,
        )
    )

    assert ledger.claims == [claim]
    assert ledger.derived_metrics["data_coverage_ratio"] == 0.91
    assert mailbox.list_messages(task_id="task-demo") == [message]
    assert mailbox.drain_for(task_id="task-demo", receiver="risk_signal") == [message]
    assert mailbox.list_messages(task_id="task-demo") == []


def test_workflow_order_keeps_quality_gate_before_risk_assessment() -> None:
    assert WORKFLOW_NODE_ORDER.index(WorkflowNodeName.DATA_QUALITY_GATE) < (
        WORKFLOW_NODE_ORDER.index(WorkflowNodeName.RISK_SIGNAL_ASSESSMENT)
    )
    assert WORKFLOW_NODE_ORDER[-1] == WorkflowNodeName.PUBLISH_ARTIFACTS


def test_api_and_cli_boundaries_are_frozen() -> None:
    assert RADAR_AGENT_API_PREFIX == "/radar-agent"
    assert full_route_paths() == (
        "/radar-agent/subjects/{subject_id}/authorizations",
        "/radar-agent/subjects/{subject_id}/authorizations/{authorization_id}/revoke",
        "/radar-agent/subjects/{subject_id}/export",
        "/radar-agent/subjects/{subject_id}/reports",
        "/radar-agent/subjects/{subject_id}/data",
        "/radar-agent/tasks",
        "/radar-agent/tasks/{task_id}/run",
        "/radar-agent/tasks/{task_id}",
        "/radar-agent/tasks/{task_id}/events",
        "/radar-agent/tasks/{task_id}/stream",
        "/radar-agent/tasks/{task_id}/confirm",
        "/radar-agent/tasks/{task_id}/confirmations/{confirmation_id}/revoke",
        "/radar-agent/chat",
    )

    parser = build_parser()
    args = parser.parse_args(
        ["run-demo", "--scenario", "frequent_out_of_bed", "--format", "jsonl"]
    )
    assert args.command == "run-demo"
    assert args.scenario == "frequent_out_of_bed"
    assert args.format == "jsonl"

    payload = build_demo_payload("frequent_out_of_bed")
    assert payload["expected"]["risk_level"] == "watch"
    assert payload["scenario"]["scenario_id"] == "frequent_out_of_bed"


def test_frontend_entrypoint_keeps_radar_primary() -> None:
    project_root = Path(__file__).resolve().parents[1]

    home_page = (project_root / "frontend/app/page.tsx").read_text()

    assert "RadarWorkspace" in home_page
    assert "LegacySleepWorkspace" not in home_page


def test_radar_namespace_does_not_import_retired_research_modules() -> None:
    radar_agent_root = Path(__file__).resolve().parents[1] / "sleepagent/radar_agent"
    for source_file in radar_agent_root.rglob("*.py"):
        source = source_file.read_text()
        assert "from sleepagent.agents" not in source
        assert "import sleepagent.agents" not in source
        assert "from sleepagent.preprocessing" not in source
        assert "from sleepagent.training" not in source
