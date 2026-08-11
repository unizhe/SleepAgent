from __future__ import annotations

import ast
from pathlib import Path

from sleepagent.product_runtime.contracts import AgentId
from sleepagent.product_runtime.episode import ProductEpisodeRuntime
from sleepagent.product_runtime.longitudinal_memory import (
    LongitudinalMemoryService,
)
from sleepagent.product_runtime.policies.care_coordination import (
    CareEscalationPolicy,
)
from sleepagent.product_runtime.policies.risk import (
    classify_structured_risk,
)
from sleepagent.product_runtime.policies.workflow import (
    CANONICAL_WORKFLOW_POLICY,
    CanonicalWorkflowPolicy,
)
from sleepagent.product_runtime.services.reviewed_knowledge import (
    ReviewedKnowledgeService,
)
from sleepagent.product_runtime.skills import default_skill_packages
from sleepagent.product_runtime.tools.artifact_rendering import (
    ArtifactRenderingTool,
)
from sleepagent.product_runtime.tools.care_coordination import (
    CareCoordinationTool,
)
from sleepagent.product_runtime.tools.knowledge_retrieval import (
    KnowledgeRetrievalTool,
)
from sleepagent.product_runtime.tools.radar_data import (
    CanonicalRadarEvidenceTool,
    RadarDataAdapter,
)
from sleepagent.product_runtime.tools.risk_classification import (
    RiskClassificationTool,
)
from sleepagent.product_runtime.tools.trend_analysis import (
    TrendAnalysisTool,
)


ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_ROOT = (
    ROOT / "sleepagent/product_runtime"
)


def test_all_legacy_capabilities_have_a_non_agent_canonical_owner() -> None:
    migrated_boundaries = {
        "radar_data": (RadarDataAdapter, CanonicalRadarEvidenceTool),
        "trend": (TrendAnalysisTool,),
        "risk": (RiskClassificationTool, classify_structured_risk),
        "knowledge": (KnowledgeRetrievalTool, ReviewedKnowledgeService),
        "report": (ArtifactRenderingTool,),
        "alert_care": (
            CareCoordinationTool,
            CareEscalationPolicy,
        ),
        "memory": (LongitudinalMemoryService,),
        "fixed_orchestrator": (
            CanonicalWorkflowPolicy,
            ProductEpisodeRuntime,
        ),
    }

    assert set(migrated_boundaries) == {
        "radar_data",
        "trend",
        "risk",
        "knowledge",
        "report",
        "alert_care",
        "memory",
        "fixed_orchestrator",
    }
    assert all(
        not any(
            getattr(boundary, "__name__", "").endswith("Agent")
            for boundary in boundaries
        )
        for boundaries in migrated_boundaries.values()
    )


def test_capability_source_has_no_legacy_agent_runtime_imports() -> None:
    forbidden_modules = (
        "sleepagent.radar_agent.agents",
        "sleepagent.radar_agent.orchestrator",
        "sleepagent.radar_agent.dynamic",
        "sleepagent.radar_agent.a2a",
    )
    forbidden_names = {
        "A2AMessage",
        "AgentResult",
        "RadarAgentName",
        "RadarDataAgent",
        "TrendAgent",
        "RiskSignalAgent",
        "RAGAgent",
        "ReportAgent",
        "AlertCareAgent",
        "MemoryAgent",
        "FixedWorkflowDecision",
        "FixedWorkflowPort",
    }
    paths = [
        path
        for folder in ("tools", "services", "policies")
        for path in (CAPABILITY_ROOT / folder).glob("*.py")
    ] + [CAPABILITY_ROOT / "episode.py"]

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        referenced_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not any(
            module.startswith(forbidden_modules)
            for module in imported_modules
        ), path
        assert not (referenced_names & forbidden_names), path


def test_real_skill_packages_are_owned_by_the_frozen_four_role_roster() -> None:
    owners = {
        package.skill_id: package.owner_agent
        for package in default_skill_packages()
    }

    assert {
        owners["interpret_longitudinal_pattern"],
        owners["draft_coordination_candidate"],
        owners["propose_memory_change"],
        owners["draft_user_material"],
    } == {
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SLEEP_CARE,
    }
    assert tuple(AgentId) == (
        AgentId.SLEEP_CARE,
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SAFETY_REVIEW,
    )


def test_product_runtime_mounts_the_canonical_workflow_policy() -> None:
    assert ProductEpisodeRuntime.workflow_policy is CANONICAL_WORKFLOW_POLICY
