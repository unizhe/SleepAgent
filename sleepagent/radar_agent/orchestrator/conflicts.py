from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import Field

from sleepagent.radar_agent.agents import AgentResult
from sleepagent.radar_agent.schemas import (
    ConflictRecord,
    EvidenceLedger,
    RadarAgentName,
    RadarAgentSchema,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
    SafetyPolicy,
)


class ExpressionBoundaryViolation(ValueError):
    pass


class ConflictResolutionOutcome(RadarAgentSchema):
    evidence_ledger: EvidenceLedger
    conflicts: list[ConflictRecord] = Field(default_factory=list)


class ConflictResolver:
    """Priority-ordered Orchestrator conflict resolver.

    The resolver is intentionally not a voting mechanism. It applies the v1
    policy order directly: data quality, evidence ledger, safety boundary, then
    conservative downgrade for unresolved risk disagreements.
    """

    def resolve(
        self,
        *,
        task_id: str,
        accepted_results: list[AgentResult],
        evidence_ledger: EvidenceLedger,
        night_summary: RadarNightSummary | None = None,
        safety_policy: SafetyPolicy | None = None,
    ) -> ConflictResolutionOutcome:
        risks = _risk_levels_by_source(accepted_results)
        conflicts: list[ConflictRecord] = []
        ledger = evidence_ledger
        decisive_conflict = False

        quality_conflict = _quality_conflict(
            task_id=task_id,
            night_summary=night_summary,
            risks=risks,
        )
        if quality_conflict is not None:
            conflicts.append(quality_conflict)
            ledger = _with_resolution(
                ledger,
                risk_level=RiskLevel.UNCERTAIN,
                uncertainty="data_quality_conflict",
                caveat=quality_conflict.decision,
            )
            decisive_conflict = True

        ledger_conflict = _evidence_ledger_conflict(
            task_id=task_id,
            evidence_ledger=ledger,
        )
        if ledger_conflict is not None:
            conflicts.append(ledger_conflict)
            ledger = _with_resolution(
                ledger,
                risk_level=_ledger_risk_level(ledger),
                uncertainty="evidence_ledger_conflict",
                caveat=ledger_conflict.decision,
            )

        if not decisive_conflict:
            safety_conflict = _safety_conflict(
                task_id=task_id,
                risks=risks,
                safety_policy=safety_policy,
            )
            if safety_conflict is not None:
                conflicts.append(safety_conflict)
                ledger = _with_resolution(
                    ledger,
                    risk_level=(
                        RiskLevel.URGENT_BOUNDARY
                        if _has_urgent_boundary(risks)
                        else _max_allowed_risk(safety_policy)
                    ),
                    uncertainty="safety_boundary_conflict",
                    caveat=safety_conflict.decision,
                )
                decisive_conflict = True

        if not decisive_conflict:
            risk_conflict = _risk_conflict(task_id=task_id, risks=risks)
            if risk_conflict is not None:
                conflicts.append(risk_conflict)
                ledger = _with_resolution(
                    ledger,
                    risk_level=RiskLevel.WATCH,
                    uncertainty="risk_conflict_unresolved",
                    caveat=risk_conflict.decision,
                )

        if conflicts:
            ledger = ledger.model_copy(
                update={"review_status": ReviewStatus.NEEDS_HUMAN_REVIEW}
            )
        return ConflictResolutionOutcome(evidence_ledger=ledger, conflicts=conflicts)


def validate_expression_agent_output(
    *,
    agent_name: RadarAgentName | str,
    evidence_ledger: EvidenceLedger,
    result: AgentResult,
) -> AgentResult:
    """Ensure ReportAgent/DialogueAgent only rewrite expression, not facts."""

    agent = agent_name.value if isinstance(agent_name, RadarAgentName) else agent_name
    if agent not in {RadarAgentName.REPORT.value, RadarAgentName.DIALOGUE.value}:
        return result
    if result.claims:
        raise ExpressionBoundaryViolation(
            "ReportAgent and DialogueAgent cannot create or modify evidence claims."
        )
    payload = result.output_payload
    if any(key in payload for key in {"claims", "evidence_claims", "evidence_ledger"}):
        raise ExpressionBoundaryViolation(
            "ReportAgent and DialogueAgent cannot emit fact or claim payloads."
        )
    ledger_risk = evidence_ledger.derived_metrics.get("risk_level")
    if "risk_level" in payload and str(payload["risk_level"]) != str(ledger_risk):
        raise ExpressionBoundaryViolation(
            "ReportAgent and DialogueAgent cannot change the ledger risk level."
        )
    allowed_refs = _allowed_expression_refs(evidence_ledger)
    unknown_refs = [ref for ref in result.evidence_refs if ref not in allowed_refs]
    if unknown_refs:
        raise ExpressionBoundaryViolation(
            "ReportAgent and DialogueAgent can only cite existing ledger refs: "
            + ", ".join(unknown_refs)
        )
    return result


def _quality_conflict(
    *,
    task_id: str,
    night_summary: RadarNightSummary | None,
    risks: dict[str, RiskLevel],
) -> ConflictRecord | None:
    if night_summary is None or night_summary.health_conclusion_allowed:
        return None
    risky_sources = [
        source
        for source, risk in risks.items()
        if _risk_rank(risk) >= _risk_rank(RiskLevel.WATCH)
    ]
    if not risky_sources:
        return None
    reason = "; ".join(night_summary.blocked_reasons) or (
        "radar data is not interpretable"
    )
    return ConflictRecord(
        conflict_id=f"conflict:{task_id}:data-quality",
        task_id=task_id,
        sources=_dedupe([RadarAgentName.RADAR_DATA.value, *risky_sources]),
        summary="Risk claims conflict with a non-interpretable radar night summary.",
        decision=(
            "Data quality has priority; downgrade final risk to uncertain because "
            f"{reason}."
        ),
        final_status="downgraded",
        requires_human_confirmation=False,
        evidence_refs=_dedupe(
            [
                night_summary.source_report_ref
                or f"night-summary:{night_summary.radar_device_id}:{night_summary.night_of.isoformat()}",
                *night_summary.source_snapshot_ids,
            ]
        ),
    )


def _evidence_ledger_conflict(
    *,
    task_id: str,
    evidence_ledger: EvidenceLedger,
) -> ConflictRecord | None:
    unsupported = [
        claim
        for claim in evidence_ledger.claims
        if claim.review_status != ReviewStatus.REVIEWED
        or not claim.evidence_refs
    ]
    if not unsupported and evidence_ledger.review_status == ReviewStatus.REVIEWED:
        return None
    if not unsupported and evidence_ledger.uncertainty not in {
        "missing_evidence_refs",
        "evidence_ledger_conflict",
    }:
        return None
    claim_ids = [claim.claim_id for claim in unsupported]
    sources = _dedupe(claim.generated_by for claim in unsupported) or [
        "evidence_ledger"
    ]
    return ConflictRecord(
        conflict_id=f"conflict:{task_id}:evidence-ledger",
        task_id=task_id,
        sources=sources,
        summary="Evidence Ledger found unsupported or unreviewed claims.",
        decision=(
            "Evidence Ledger has priority; unsupported claims are downgraded "
            f"or rejected before report, dialogue, alert, or doctor material generation: {claim_ids}."
        ),
        final_status="downgraded",
        requires_human_confirmation=True,
        evidence_refs=_dedupe(
            list(evidence_ledger.raw_evidence_refs)
            + list(evidence_ledger.canonical_evidence_refs)
            + claim_ids
        ),
    )


def _safety_conflict(
    *,
    task_id: str,
    risks: dict[str, RiskLevel],
    safety_policy: SafetyPolicy | None,
) -> ConflictRecord | None:
    if _has_urgent_boundary(risks):
        urgent_sources = [
            source for source, risk in risks.items() if risk == RiskLevel.URGENT_BOUNDARY
        ]
        return ConflictRecord(
            conflict_id=f"conflict:{task_id}:safety-boundary",
            task_id=task_id,
            sources=urgent_sources,
            summary="Safety boundary risk conflicts with lower-severity outputs.",
            decision=(
                "Safety boundary has priority; preserve urgent_boundary and "
                "require human confirmation before external action."
            ),
            final_status="accepted",
            requires_human_confirmation=True,
            evidence_refs=[],
        )
    if safety_policy is None:
        return None
    max_allowed = safety_policy.max_risk_level
    violating = {
        source: risk
        for source, risk in risks.items()
        if _risk_rank(risk) > _risk_rank(max_allowed)
    }
    if not violating:
        return None
    return ConflictRecord(
        conflict_id=f"conflict:{task_id}:safety-cap",
        task_id=task_id,
        sources=list(violating),
        summary="Agent risk output exceeds the active safety policy.",
        decision=(
            f"Safety policy caps risk at {max_allowed.value}; higher output is downgraded."
        ),
        final_status="downgraded",
        requires_human_confirmation=True,
        evidence_refs=[],
    )


def _risk_conflict(
    *,
    task_id: str,
    risks: dict[str, RiskLevel],
) -> ConflictRecord | None:
    decision_risks = {
        source: risk
        for source, risk in risks.items()
        if source not in {RadarAgentName.RADAR_DATA.value, RadarAgentName.TREND.value}
    }
    non_info = {
        source: risk
        for source, risk in decision_risks.items()
        if risk not in {RiskLevel.INFO, RiskLevel.UNCERTAIN}
    }
    if len(set(non_info.values())) <= 1:
        return None
    return ConflictRecord(
        conflict_id=f"conflict:{task_id}:risk-disagreement",
        task_id=task_id,
        sources=list(non_info),
        summary="Agent risk levels disagree and cannot be resolved by evidence priority.",
        decision=(
            "No majority vote is used; unresolved risk disagreement is marked "
            "uncertain and conservatively downgraded to watch with a follow-up suggestion."
        ),
        final_status="uncertain",
        requires_human_confirmation=True,
        evidence_refs=[],
    )


def _risk_levels_by_source(results: Iterable[AgentResult]) -> dict[str, RiskLevel]:
    risks: dict[str, RiskLevel] = {}
    for result in results:
        source = result.agent_name.value
        risk = _risk_from_result(result)
        if risk is not None:
            risks[source] = risk
    return risks


def _risk_from_result(result: AgentResult) -> RiskLevel | None:
    payload_risk = _risk_from_payload(result.output_payload)
    if payload_risk is not None:
        return payload_risk
    if result.claims:
        return max((claim.risk_level for claim in result.claims), key=_risk_rank)
    return None


def _risk_from_payload(payload: dict[str, Any]) -> RiskLevel | None:
    for key in ("risk_level",):
        if key in payload:
            return _risk_or_none(payload[key])
    risk_signal = payload.get("risk_signal")
    if isinstance(risk_signal, dict):
        return _risk_or_none(risk_signal.get("risk_level"))
    return None


def _risk_or_none(value: Any) -> RiskLevel | None:
    if value is None:
        return None
    try:
        return RiskLevel(str(value))
    except ValueError:
        return None


def _with_resolution(
    ledger: EvidenceLedger,
    *,
    risk_level: RiskLevel,
    uncertainty: str,
    caveat: str,
) -> EvidenceLedger:
    derived_metrics = dict(ledger.derived_metrics)
    derived_metrics["risk_level"] = risk_level.value
    caveats = list(ledger.caveats)
    _append_unique(caveats, caveat)
    uncertainty_value = _join_uncertainties(ledger.uncertainty, uncertainty)
    return ledger.model_copy(
        update={
            "derived_metrics": derived_metrics,
            "uncertainty": uncertainty_value,
            "caveats": caveats,
        }
    )


def _ledger_risk_level(ledger: EvidenceLedger) -> RiskLevel:
    return _risk_or_none(ledger.derived_metrics.get("risk_level")) or RiskLevel.UNCERTAIN


def _max_allowed_risk(safety_policy: SafetyPolicy | None) -> RiskLevel:
    return safety_policy.max_risk_level if safety_policy else RiskLevel.ESCALATE


def _has_urgent_boundary(risks: dict[str, RiskLevel]) -> bool:
    return RiskLevel.URGENT_BOUNDARY in set(risks.values())


def _risk_rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.INFO: 0,
        RiskLevel.UNCERTAIN: 1,
        RiskLevel.WATCH: 2,
        RiskLevel.ESCALATE: 3,
        RiskLevel.URGENT_BOUNDARY: 4,
    }[risk]


def _allowed_expression_refs(ledger: EvidenceLedger) -> set[str]:
    return (
        set(ledger.raw_evidence_refs)
        | set(ledger.canonical_evidence_refs)
        | {claim.claim_id for claim in ledger.claims}
        | {ref for claim in ledger.claims for ref in claim.evidence_refs}
    )


def _dedupe(items: Iterable[str | None]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _join_uncertainties(existing: str | None, new: str) -> str:
    values = [part.strip() for part in (existing or "").split(";") if part.strip()]
    _append_unique(values, new)
    return "; ".join(values)


__all__ = [
    "ConflictResolutionOutcome",
    "ConflictResolver",
    "ExpressionBoundaryViolation",
    "validate_expression_agent_output",
]
