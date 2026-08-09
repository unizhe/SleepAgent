from __future__ import annotations

import inspect

from sleepagent.radar_agent.product_agent.episode import ProductEpisodeRuntime
from sleepagent.radar_agent.product_agent.policies.workflow import (
    CANONICAL_WORKFLOW_POLICY,
    CanonicalWorkflowInvariant,
)
from tests.golden_fixtures import load_phase3a_capability_goldens


def test_legacy_order_characterizes_deterministic_rules_worth_preserving() -> None:
    node_order = load_phase3a_capability_goldens()["fixed_runtime_policy"][
        "node_order"
    ]
    legacy_positions = {
        item: index for index, item in enumerate(node_order)
    }

    assert legacy_positions["DataQualityGate"] < legacy_positions[
        "EvidenceLedgerReview"
    ]
    assert legacy_positions["RiskSignalAssessment"] < legacy_positions[
        "EvidenceLedgerReview"
    ]
    assert legacy_positions["EvidenceLedgerReview"] < legacy_positions[
        "RoleReportGeneration"
    ]
    assert legacy_positions["HumanConfirmation"] < legacy_positions[
        "PublishArtifacts"
    ]


def test_canonical_policy_preserves_rules_without_preserving_workflow_nodes() -> None:
    assert CANONICAL_WORKFLOW_POLICY.invariants == tuple(
        CanonicalWorkflowInvariant
    )
    assert set(CANONICAL_WORKFLOW_POLICY.invariants) == {
        CanonicalWorkflowInvariant.SAFETY_PREEMPTS_MODEL,
        CanonicalWorkflowInvariant.FACTS_BEFORE_EVIDENCE,
        CanonicalWorkflowInvariant.ACCEPTED_EVIDENCE_BEFORE_CARE,
        CanonicalWorkflowInvariant.SAFETY_BEFORE_RESTRICTED_PUBLICATION,
        CanonicalWorkflowInvariant.CONFIRMATION_BEFORE_STATE_CHANGE,
        CanonicalWorkflowInvariant.SINGLE_WRITER_COMMIT,
        CanonicalWorkflowInvariant.FAIL_CLOSED_ON_REQUIRED_TOOL_ERROR,
        CanonicalWorkflowInvariant.CHECKPOINT_REVALIDATES_BINDING,
        CanonicalWorkflowInvariant.CONTEXT_IS_ROLE_SCOPED,
        CanonicalWorkflowInvariant.EXACT_TARGET_CONFLICT_REJECTION,
    }
    assert ProductEpisodeRuntime.workflow_policy is CANONICAL_WORKFLOW_POLICY


def test_canonical_workflow_policy_has_no_legacy_dto_or_runtime_dependency() -> None:
    source = inspect.getsource(
        __import__(
            "sleepagent.radar_agent.product_agent.policies.workflow",
            fromlist=["CanonicalWorkflowPolicy"],
        )
    )

    for forbidden in (
        "AgentResult",
        "A2AMessage",
        "A2ACollaborationRound",
        "RadarAgentName",
        "FixedWorkflowDecision",
        "FixedWorkflowPort",
        "sleepagent.radar_agent.agents",
        "sleepagent.radar_agent.orchestrator",
        "sleepagent.radar_agent.dynamic",
    ):
        assert forbidden not in source
