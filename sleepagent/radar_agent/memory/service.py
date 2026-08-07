from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from sleepagent.radar_agent.persistence.models import RadarMemorySummary
from sleepagent.radar_agent.schemas import (
    ContextPacket,
    HumanConfirmationRequest,
    MemoryCandidate,
    RoleReportArtifact,
)

from .contracts import (
    CareEventMemory,
    LongTermTrendMemory,
    MemoryPrivacyDecision,
    MemoryWriteDecision,
    ShortTermDialogueTurn,
    ShortTermMemoryContext,
    UserPreferenceMemory,
)


_ALLOWED_PAYLOAD_FIELDS = {
    "trend": {"windows", "risk_level", "risk_signal_change"},
    "preference": {"expression_style", "family_focus", "doctor_report_format"},
    "care_event": {"event_type", "status", "occurred_at", "reference_id"},
}
_FORBIDDEN_KEYS = {
    "raw_radar",
    "raw_radar_stream",
    "radar_raw_event",
    "raw_events",
    "vendor_payload",
    "full_conversation",
    "medication_free_text",
    "medication_notes",
    "family_conflict",
    "financial_info",
    "bank_account",
}
_FORBIDDEN_TAGS = {
    "raw_radar",
    "medication_free_text",
    "family_conflict",
    "financial",
}
_FORBIDDEN_TEXT = (
    "raw radar",
    "raw_radar",
    "radar raw event",
    "vendor payload",
    "medication free text",
    "medication",
    "drug detail",
    "药物自由文本",
    "用药详情",
    "用药",
    "服药",
    "剂量",
    "毫克",
    "family conflict",
    "家庭矛盾",
    "家庭争吵",
    "financial info",
    "财务信息",
    "bank account",
    "银行账户",
    "账户余额",
)
_RAW_EVIDENCE_PREFIXES = ("raw:", "raw-event:", "radar-raw:", "vendor-event:")
_PAYLOAD_MODELS = {
    "trend": LongTermTrendMemory,
    "preference": UserPreferenceMemory,
    "care_event": CareEventMemory,
}


def build_short_term_memory(
    context: ContextPacket,
    *,
    dialogue_turns: list[ShortTermDialogueTurn] | None = None,
    current_report_artifact: RoleReportArtifact | None = None,
) -> ShortTermMemoryContext:
    summaries = context.evidence_packet.night_summaries
    if current_report_artifact is None:
        raw_report = context.evidence_packet.data_quality.get(
            "current_report_artifact"
        )
        if raw_report:
            current_report_artifact = RoleReportArtifact.model_validate(raw_report)
    if dialogue_turns is None:
        raw_turns = context.evidence_packet.data_quality.get(
            "current_dialogue_turns", []
        )
        dialogue_turns = [
            ShortTermDialogueTurn.model_validate(item) for item in raw_turns
        ]
    return ShortTermMemoryContext(
        task_id=context.task_context.task_id,
        trace_id=context.task_context.trace_id,
        latest_night_summary=summaries[-1] if summaries else None,
        dialogue_turns=dialogue_turns,
        current_report_artifact=current_report_artifact,
    )


class MemoryPrivacyFilter:
    """Strict allowlist for data eligible for long-term memory."""

    def review(self, candidate: MemoryCandidate) -> MemoryPrivacyDecision:
        reasons: list[str] = []
        keys = _nested_keys(candidate.payload)
        forbidden_keys = sorted(keys & _FORBIDDEN_KEYS)
        if forbidden_keys:
            reasons.append("forbidden_fields:" + ",".join(forbidden_keys))
        forbidden_tags = sorted(set(candidate.privacy_tags) & _FORBIDDEN_TAGS)
        if forbidden_tags:
            reasons.append("forbidden_privacy_tags:" + ",".join(forbidden_tags))
        if (
            "sensitive_dialogue" in candidate.privacy_tags
            and not candidate.authorization_refs
        ):
            reasons.append("unauthorized_sensitive_dialogue")
        forbidden_text = sorted(
            _forbidden_text_matches(candidate.summary, candidate.payload)
        )
        if forbidden_text:
            reasons.append("forbidden_content:" + ",".join(forbidden_text))
        raw_refs = sorted(
            ref
            for ref in candidate.evidence_refs
            if ref.lower().startswith(_RAW_EVIDENCE_PREFIXES)
        )
        if raw_refs:
            reasons.append("raw_evidence_refs_not_allowed")
        allowed_fields = _ALLOWED_PAYLOAD_FIELDS[candidate.memory_type]
        unexpected = sorted(set(candidate.payload) - allowed_fields)
        if unexpected:
            reasons.append("unexpected_payload_fields:" + ",".join(unexpected))
        if candidate.memory_type == "trend":
            windows = candidate.payload.get("windows", {})
            if set(windows) != {"7", "30", "90"}:
                reasons.append("missing_7_30_90_windows")
        try:
            _PAYLOAD_MODELS[candidate.memory_type].model_validate(candidate.payload)
        except ValidationError:
            reasons.append("invalid_structured_payload")
        return MemoryPrivacyDecision(
            allowed=not reasons,
            reasons=reasons,
            sanitized_payload=deepcopy(candidate.payload) if not reasons else {},
        )


class OrchestratedMemoryWriter:
    """Orchestrator policy decision only; persistence belongs to TaskService."""

    def __init__(self, privacy_filter: MemoryPrivacyFilter | None = None) -> None:
        self.privacy_filter = privacy_filter or MemoryPrivacyFilter()

    def decide(
        self,
        candidate: MemoryCandidate,
        confirmation: HumanConfirmationRequest | None,
        *,
        actor: str,
    ) -> MemoryWriteDecision:
        if actor != "orchestrator":
            raise PermissionError(
                "only the Orchestrator may decide long-term memory writes"
            )
        privacy = self.privacy_filter.review(candidate)
        reasons = list(privacy.reasons)
        if confirmation is None:
            reasons.append("family_confirmation_required")
        else:
            if confirmation.task_id != candidate.task_id:
                reasons.append("confirmation_task_mismatch")
            if confirmation.action_type != candidate.confirmation_action:
                reasons.append("confirmation_action_mismatch")
            if confirmation.requested_role != "family":
                reasons.append("family_confirmation_required")
            if confirmation.status != "approved" or not confirmation.resolved_by:
                reasons.append("confirmation_not_approved")
        if reasons:
            return MemoryWriteDecision(
                candidate_id=candidate.candidate_id,
                task_id=candidate.task_id,
                reasons=list(dict.fromkeys(reasons)),
                confirmation_id=confirmation.confirmation_id if confirmation else None,
                candidate=candidate,
            )
        memory = RadarMemorySummary(
            memory_summary_id=f"memory-summary:{candidate.candidate_id}",
            subject_id=candidate.subject_id,
            task_id=candidate.task_id,
            memory_type=candidate.memory_type,
            summary=candidate.summary,
            payload=privacy.sanitized_payload,
            evidence_refs=candidate.evidence_refs,
            source_candidate_id=candidate.candidate_id,
            confirmation_id=confirmation.confirmation_id,
            privacy_reviewed=True,
            generated_at=datetime.now(timezone.utc),
        )
        return MemoryWriteDecision(
            candidate_id=candidate.candidate_id,
            task_id=candidate.task_id,
            approved_for_write=True,
            confirmation_id=confirmation.confirmation_id,
            candidate=candidate,
            memory=memory,
        )


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for item in value.values()
            for nested in _nested_keys(item)
        }
    if isinstance(value, (list, tuple)):
        return {nested for item in value for nested in _nested_keys(item)}
    return set()


def _forbidden_text_matches(*values: Any) -> set[str]:
    strings: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value.lower())
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for value in values:
        collect(value)
    return {
        pattern
        for pattern in _FORBIDDEN_TEXT
        if any(pattern in item for item in strings)
    }


__all__ = [
    "MemoryPrivacyFilter",
    "OrchestratedMemoryWriter",
    "build_short_term_memory",
]
