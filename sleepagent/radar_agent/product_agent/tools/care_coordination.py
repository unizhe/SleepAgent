from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    StrictContract,
)
from sleepagent.radar_agent.product_agent.policies.care_coordination import (
    CareEscalationPolicy,
    CareRoutingDecision,
    RiskLevelValue,
)
CARE_COORDINATION_TOOL_VERSION = "sleepagent-care-coordination-tool.v1"


class CareRiskReceiptBinding(StrictContract):
    """Accepted Risk Tool decision made visible to Care coordination."""

    risk_receipt_ref: str = Field(..., min_length=1)
    risk_level: Literal[
        "normal",
        "watch",
        "escalate",
        "uncertain",
        "urgent_boundary",
    ]
    quality_status: Literal[
        "good",
        "partial",
        "unusable",
        "missing",
        "usable",
        "limited",
    ]
    urgent_required: bool = False
    source_refs: tuple[str, ...] = ()


class CareCoordinationPolicyRequest(StrictContract):
    """Runtime-bound accepted Evidence and Risk decisions for Care policy."""

    coordination_policy_ref: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    risk_decisions: tuple[CareRiskReceiptBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def bind_accepted_inputs(self) -> "CareCoordinationPolicyRequest":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "Care coordination Evidence reference does not bind its hash"
            )
        refs = [item.risk_receipt_ref for item in self.risk_decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("Care coordination Risk receipts must be unique")
        return self


class CareCoordinationPolicyResult(StrictContract):
    tool_version: Literal["sleepagent-care-coordination-tool.v1"] = (
        CARE_COORDINATION_TOOL_VERSION
    )
    coordination_policy_ref: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    risk_receipt_refs: tuple[str, ...] = Field(min_length=1)
    family_notification_requires_candidate: Literal[True] = True
    routing: CareRoutingDecision
    source_refs: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def bind_result_sources(self) -> "CareCoordinationPolicyResult":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "Care coordination result does not bind accepted Evidence"
            )
        required_prefix = list(
            dict.fromkeys(
                [
                    self.coordination_policy_ref,
                    self.accepted_evidence_ref,
                    *self.risk_receipt_refs,
                ]
            )
        )
        if self.source_refs[: len(required_prefix)] != required_prefix:
            raise ValueError(
                "Care coordination sources must preserve policy, Evidence, "
                "and Risk receipt bindings"
            )
        return self


class CareCoordinationTool:
    """Read deterministic coordination policy without executing an action."""

    def read_policy(
        self,
        request: CareCoordinationPolicyRequest,
    ) -> CareCoordinationPolicyResult:
        if type(request) is not CareCoordinationPolicyRequest:
            raise TypeError(
                "CareCoordinationTool requires CareCoordinationPolicyRequest"
            )
        risk_level = _risk_level(request.risk_decisions)
        data_quality = _quality_status(request.risk_decisions)
        routing = CareEscalationPolicy().evaluate(
            risk_level=risk_level,
            data_quality_status=data_quality,
        )
        risk_receipt_refs = tuple(
            item.risk_receipt_ref for item in request.risk_decisions
        )
        refs = list(
            dict.fromkeys(
                [
                    request.coordination_policy_ref,
                    request.accepted_evidence_ref,
                    *risk_receipt_refs,
                    *(
                        ref
                        for decision in request.risk_decisions
                        for ref in decision.source_refs
                    ),
                ]
            )
        )
        return CareCoordinationPolicyResult(
            coordination_policy_ref=request.coordination_policy_ref,
            accepted_evidence_ref=request.accepted_evidence_ref,
            accepted_evidence_hash=request.accepted_evidence_hash,
            risk_receipt_refs=risk_receipt_refs,
            routing=routing,
            source_refs=refs,
        )


def _risk_level(
    values: tuple[CareRiskReceiptBinding, ...],
) -> RiskLevelValue:
    if any(
        item.urgent_required or item.risk_level == "urgent_boundary"
        for item in values
    ):
        return "urgent_boundary"
    if any(item.risk_level == "escalate" for item in values):
        return "escalate"
    if any(item.risk_level == "watch" for item in values):
        return "watch"
    if all(item.risk_level == "normal" for item in values):
        return "info"
    return "uncertain"


def _quality_status(values: tuple[CareRiskReceiptBinding, ...]) -> str:
    qualities = {item.quality_status for item in values}
    if qualities.intersection({"unusable", "missing"}):
        return "unusable"
    if qualities.intersection({"partial", "limited"}):
        return "partial"
    return "good"


__all__ = [
    "CARE_COORDINATION_TOOL_VERSION",
    "CareCoordinationPolicyRequest",
    "CareCoordinationPolicyResult",
    "CareRiskReceiptBinding",
    "CareCoordinationTool",
]
