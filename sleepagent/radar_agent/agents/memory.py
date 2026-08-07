from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError

from sleepagent.radar_agent.confirmation import confirmation_request
from sleepagent.radar_agent.memory import (
    CareEventMemory,
    LongTermTrendMemory,
    TrendWindowMemory,
    UserPreferenceMemory,
)
from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidenceLedger,
    HumanConfirmationRequest,
    MemoryCandidate,
    RadarAgentName,
    RadarAgentSchema,
    RiskLevel,
)


class MemoryProposal(RadarAgentSchema):
    task_id: str
    candidates: list[MemoryCandidate] = Field(default_factory=list)
    confirmation_requests: list[HumanConfirmationRequest] = Field(default_factory=list)
    rejected_inputs: list[str] = Field(default_factory=list)
    write_performed: bool = False


class MemoryAgent:
    """Creates structured candidates only; it cannot approve or persist memory."""

    name = RadarAgentName.MEMORY

    def run(self, context: ContextPacket) -> AgentResult:
        ledger = _require_ledger(context)
        subject_id = _subject_id(context)
        refs = _ledger_refs(ledger)
        candidates: list[MemoryCandidate] = []
        rejected_inputs: list[str] = []
        risk = str(ledger.derived_metrics.get("risk_level", RiskLevel.INFO.value))
        quality = str(ledger.derived_metrics.get("data_quality_status", "unknown"))
        memory_block = (
            "data_quality_unusable"
            if quality == "unusable"
            else (
                "urgent_boundary"
                if risk == RiskLevel.URGENT_BOUNDARY.value
                else None
            )
        )
        if memory_block:
            rejected_inputs.append(memory_block)
        if subject_id and refs and memory_block is None:
            candidates.append(_trend_candidate(context, ledger, subject_id, refs))
            preference, preference_error = _preference_candidate(
                context, subject_id, refs
            )
            if preference is not None:
                candidates.append(preference)
            if preference_error:
                rejected_inputs.append(preference_error)
            care_candidates, care_errors = _care_event_candidates(
                context, subject_id, refs
            )
            candidates.extend(care_candidates)
            rejected_inputs.extend(care_errors)
        confirmations = [_confirmation(candidate) for candidate in candidates]
        proposal = MemoryProposal(
            task_id=context.task_context.task_id,
            candidates=candidates,
            confirmation_requests=confirmations,
            rejected_inputs=rejected_inputs,
        )
        return AgentResult(
            agent_name=self.name,
            evidence_refs=refs,
            confidence=ledger.confidence if candidates else 0,
            uncertainties=([] if subject_id else ["subject_id_missing"]) + rejected_inputs,
            safety_flags=[
                "candidate_only",
                "no_long_term_write",
                "privacy_minimized",
                "structured_memory_layers",
            ],
            candidate_actions=(
                ["request_memory_write_confirmation"] if candidates else []
            ),
            output_payload={"memory_proposal": proposal.model_dump(mode="json")},
        )


def _trend_candidate(
    context: ContextPacket,
    ledger: EvidenceLedger,
    subject_id: str,
    refs: list[str],
) -> MemoryCandidate:
    raw = context.evidence_packet.data_quality.get("trend_result", {})
    windows = {
        key: _trend_window(key, raw.get("windows", {}).get(key), context, refs)
        for key in ("7", "30", "90")
    }
    trend = LongTermTrendMemory(
        windows=windows,
        risk_level=str(ledger.derived_metrics.get("risk_level", "info")),
        risk_signal_change=str(
            context.evidence_packet.data_quality.get(
                "risk_signal_change",
                ledger.derived_metrics.get(
                    "risk_signal_change", "insufficient_history"
                ),
            )
        ),
    )
    computed = [key for key, item in windows.items() if item.status == "computed"]
    summary = (
        f"已形成 {','.join(computed)} 天结构化趋势摘要。"
        if computed
        else "7/30/90 天趋势样本暂不足，保留结构化观察基线。"
    )
    return MemoryCandidate(
        candidate_id=f"memory:{context.task_context.task_id}:trend",
        subject_id=subject_id,
        task_id=context.task_context.task_id,
        memory_type="trend",
        summary=summary,
        payload=_strip_standard_mappings(
            trend.model_dump(mode="json", exclude={"standard_mappings"})
        ),
        evidence_refs=refs,
        privacy_tags=["canonical_summary_only"],
    )


def _trend_window(
    key: str,
    raw_metrics: Any,
    context: ContextPacket,
    refs: list[str],
) -> TrendWindowMemory:
    metrics: dict[str, Any] = {}
    evidence_refs: list[str] = []
    statuses: set[str] = set()
    if isinstance(raw_metrics, list):
        for item in raw_metrics:
            if not isinstance(item, dict) or not item.get("metric_name"):
                continue
            metrics[str(item["metric_name"])] = item.get("value")
            statuses.add(str(item.get("status", "insufficient_data")))
            evidence_refs.extend(str(ref) for ref in item.get("evidence_refs", []))
    status = (
        "computed"
        if "computed" in statuses
        else (
            "not_interpretable"
            if "not_interpretable" in statuses
            else "insufficient_data"
        )
    )
    if not metrics and context.evidence_packet.night_summaries:
        latest = context.evidence_packet.night_summaries[-1]
        metrics = {
            "sleep_minutes": latest.total_sleep_minutes,
            "out_of_bed_count": latest.out_of_bed_count,
            "movement_count": latest.movement_count,
            "data_coverage_ratio": latest.data_coverage_ratio,
        }
    return TrendWindowMemory(
        window_days=int(key),
        status=status,
        metrics=metrics,
        evidence_refs=list(dict.fromkeys(evidence_refs or refs)),
    )


def _preference_candidate(
    context: ContextPacket,
    subject_id: str,
    refs: list[str],
) -> tuple[MemoryCandidate | None, str | None]:
    raw = context.evidence_packet.data_quality.get("preference_updates")
    if not raw:
        return None, None
    try:
        preference = UserPreferenceMemory.model_validate(raw)
    except ValidationError:
        return None, "invalid_preference_update"
    if not any(
        [
            preference.expression_style,
            preference.family_focus,
            preference.doctor_report_format,
        ]
    ):
        return None, None
    return MemoryCandidate(
        candidate_id=f"memory:{context.task_context.task_id}:preference",
        subject_id=subject_id,
        task_id=context.task_context.task_id,
        memory_type="preference",
        summary="已提出用户表达与报告偏好更新。",
        payload=preference.model_dump(
            mode="json", exclude_none=True, exclude={"standard_mappings"}
        ),
        evidence_refs=refs,
        privacy_tags=["confirmed_preference_candidate"],
    ), None


def _care_event_candidates(
    context: ContextPacket,
    subject_id: str,
    refs: list[str],
) -> tuple[list[MemoryCandidate], list[str]]:
    values: list[MemoryCandidate] = []
    errors: list[str] = []
    raw_events = context.evidence_packet.data_quality.get("care_events", [])
    if not isinstance(raw_events, list):
        return [], ["invalid_care_events"]
    for index, raw in enumerate(raw_events):
        try:
            event = CareEventMemory.model_validate(raw)
        except ValidationError:
            errors.append(f"invalid_care_event:{index}")
            continue
        action = {
            "watch_reminder": "enable_persistent_family_reminder",
            "care_plan": "enable_care_plan",
        }.get(event.event_type, "write_long_term_memory")
        values.append(
            MemoryCandidate(
                candidate_id=(
                    f"memory:{context.task_context.task_id}:care:{index}:"
                    f"{event.event_type}"
                ),
                subject_id=subject_id,
                task_id=context.task_context.task_id,
                memory_type="care_event",
                summary=f"照护事件候选：{event.event_type}（{event.status}）。",
                payload=event.model_dump(mode="json", exclude={"standard_mappings"}),
                evidence_refs=refs,
                privacy_tags=["structured_care_event"],
                confirmation_action=action,
            )
        )
    return values, errors


def _confirmation(candidate: MemoryCandidate) -> HumanConfirmationRequest:
    return confirmation_request(
        task_id=candidate.task_id,
        action_type=candidate.confirmation_action,
        reason=f"{candidate.memory_type} long-term memory requires family confirmation.",
        evidence_refs=candidate.evidence_refs,
        scope_id=candidate.candidate_id,
    )


def _require_ledger(context: ContextPacket) -> EvidenceLedger:
    if context.evidence_packet.evidence_ledger is None:
        raise ValueError("MemoryAgent requires an EvidenceLedger.")
    return context.evidence_packet.evidence_ledger


def _subject_id(context: ContextPacket) -> str | None:
    summaries = context.evidence_packet.night_summaries
    if summaries and summaries[-1].subject_id:
        return summaries[-1].subject_id
    value = context.evidence_packet.data_quality.get("subject_id")
    return str(value) if value else None


def _ledger_refs(ledger: EvidenceLedger) -> list[str]:
    return list(
        dict.fromkeys(
            ledger.canonical_evidence_refs
            + [ref for claim in ledger.claims for ref in claim.evidence_refs]
        )
    )


def _strip_standard_mappings(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_standard_mappings(item)
            for key, item in value.items()
            if key != "standard_mappings"
        }
    if isinstance(value, list):
        return [_strip_standard_mappings(item) for item in value]
    return value


__all__ = ["MemoryAgent", "MemoryProposal"]
