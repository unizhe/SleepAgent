from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


CANONICAL_WORKFLOW_POLICY_VERSION = "sleepagent-canonical-workflow-policy.v1"


class CanonicalWorkflowInvariant(str, Enum):
    """Deterministic ordering and authority rules for Product Episodes."""

    SAFETY_PREEMPTS_MODEL = "safety_preempts_model"
    FACTS_BEFORE_EVIDENCE = "facts_before_evidence"
    ACCEPTED_EVIDENCE_BEFORE_CARE = "accepted_evidence_before_care"
    SAFETY_BEFORE_RESTRICTED_PUBLICATION = (
        "safety_before_restricted_publication"
    )
    CONFIRMATION_BEFORE_STATE_CHANGE = "confirmation_before_state_change"
    SINGLE_WRITER_COMMIT = "single_writer_commit"
    FAIL_CLOSED_ON_REQUIRED_TOOL_ERROR = "fail_closed_on_required_tool_error"
    CHECKPOINT_REVALIDATES_BINDING = "checkpoint_revalidates_binding"
    CONTEXT_IS_ROLE_SCOPED = "context_is_role_scoped"
    EXACT_TARGET_CONFLICT_REJECTION = "exact_target_conflict_rejection"


@dataclass(frozen=True, slots=True)
class CanonicalWorkflowPolicy:
    """Immutable policy mounted by the canonical Product Episode Runtime.

    The Runtime and governance layer enforce these invariants.  This policy is
    intentionally not a workflow-node roster and carries no Agent/A2A payloads.
    """

    version: str = CANONICAL_WORKFLOW_POLICY_VERSION
    invariants: tuple[CanonicalWorkflowInvariant, ...] = tuple(
        CanonicalWorkflowInvariant
    )


CANONICAL_WORKFLOW_POLICY = CanonicalWorkflowPolicy()


__all__ = [
    "CANONICAL_WORKFLOW_POLICY",
    "CANONICAL_WORKFLOW_POLICY_VERSION",
    "CanonicalWorkflowInvariant",
    "CanonicalWorkflowPolicy",
]
