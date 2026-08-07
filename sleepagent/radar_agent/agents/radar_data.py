from __future__ import annotations

from datetime import date, datetime

from sleepagent.radar_agent.quality import DataQualityGate, RadarProviderLike
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    AgentResult,
    ContextPacket,
    EvidenceClaim,
    RadarAgentName,
    RadarDataQualityStatus,
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
    ReviewStatus,
    RiskLevel,
)


class RadarDataAgent:
    name = RadarAgentName.RADAR_DATA

    def __init__(
        self,
        provider: RadarProviderLike,
        *,
        quality_gate: DataQualityGate | None = None,
    ) -> None:
        self.provider = provider
        self.quality_gate = quality_gate or DataQualityGate()

    def run(self, context: ContextPacket) -> AgentResult:
        device, snapshots, provider_report = self.ingest(context)
        night_summary = self.quality_gate.run(
            device=device,
            snapshots=snapshots,
            provider_report=provider_report,
            night_of=_requested_night(context),
        )
        return self.build_result(
            context,
            device=device,
            snapshots=snapshots,
            night_summary=night_summary,
        )

    def ingest(
        self,
        context: ContextPacket,
    ) -> tuple[RadarDevice, list[RadarVitalSnapshot], RadarNightSummary | None]:
        """Pull provider data without bypassing the canonical provider contract."""

        return self._pull_canonical_data(context)

    def build_result(
        self,
        context: ContextPacket,
        *,
        device: RadarDevice,
        snapshots: list[RadarVitalSnapshot],
        night_summary: RadarNightSummary,
    ) -> AgentResult:
        """Build the unified result after normalization and quality gating."""

        evidence_refs = _dedupe(
            [_summary_ref(night_summary)]
            + [f"snapshot:{snapshot.snapshot_id}" for snapshot in snapshots]
        )
        claim = EvidenceClaim(
            claim_id=f"radar-data:{context.task_context.task_id}:{night_summary.night_of.isoformat()}",
            task_id=context.task_context.task_id,
            text="Radar provider data was normalized into canonical night evidence and quality gated.",
            evidence_refs=[_summary_ref(night_summary)],
            confidence=_quality_confidence(night_summary),
            risk_level=RiskLevel.INFO,
            uncertainty=(
                "radar_data_not_interpretable"
                if night_summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
                else None
            ),
            caveats=night_summary.caveats,
            generated_by=self.name.value,
            review_status=ReviewStatus.REVIEWED,
        )
        return AgentResult(
            agent_name=self.name,
            claims=[claim],
            evidence_refs=evidence_refs,
            confidence=claim.confidence,
            uncertainties=night_summary.blocked_reasons + night_summary.quality_reasons,
            safety_flags=[
                "canonical_radar_only",
                "quality_gate_completed",
                "no_global_state_write",
            ],
            candidate_actions=(
                ["show_data_collection_guidance"]
                if night_summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
                else []
            ),
            next_requests=[
                A2AMessage(
                    message_id=(
                        f"a2a:{context.task_context.task_id}:radar-data-to-trend"
                    ),
                    sender=self.name.value,
                    receiver=RadarAgentName.TREND.value,
                    task_id=context.task_context.task_id,
                    intent="analyze_canonical_night_trend",
                    evidence_refs=evidence_refs,
                    confidence=claim.confidence,
                    requested_action="compute_7_30_90_day_trends",
                    risk_level=RiskLevel.INFO,
                    collaboration_round=1,
                )
            ],
            output_payload={
                "device": device.model_dump(mode="json"),
                "snapshot_count": len(snapshots),
                "night_summary": night_summary.model_dump(mode="json"),
            },
        )

    def _pull_canonical_data(
        self,
        context: ContextPacket,
    ) -> tuple[RadarDevice, list[RadarVitalSnapshot], RadarNightSummary | None]:
        radar_device_id = _requested_device_id(context)
        if radar_device_id is None:
            devices = self.provider.list_devices()
            if not devices:
                raise ValueError("Radar provider returned no devices.")
            radar_device_id = devices[0].radar_device_id
        device = self.provider.get_device(radar_device_id)
        night_of = _requested_night(context)
        provider_report = self.provider.pull_night_report(
            device.radar_device_id,
            night_of=night_of,
        )
        snapshots = self.provider.pull_snapshots(device.radar_device_id)
        return device, snapshots, provider_report


def _requested_device_id(context: ContextPacket) -> str | None:
    value = context.evidence_packet.data_quality.get("radar_device_id")
    return str(value) if value else None


def _requested_night(context: ContextPacket) -> date | datetime | None:
    value = context.evidence_packet.data_quality.get("night_of")
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return None


def _quality_confidence(summary: RadarNightSummary) -> float:
    confidence = summary.data_coverage_ratio
    if summary.data_quality_status == RadarDataQualityStatus.PARTIAL:
        confidence = min(confidence, 0.65)
    if summary.data_quality_status == RadarDataQualityStatus.UNUSABLE:
        confidence = min(confidence, 0.35)
    return round(max(0.0, min(1.0, confidence)), 2)


def _summary_ref(summary: RadarNightSummary) -> str:
    if summary.source_report_ref:
        return summary.source_report_ref
    return f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


__all__ = ["RadarDataAgent"]
