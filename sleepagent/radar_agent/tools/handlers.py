from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from sleepagent.radar_agent.agents import ReportAgent, TrendAgent
from sleepagent.radar_agent.agents.alert_care import AlertCareAgent
from sleepagent.radar_agent.quality import DataQualityGate, RadarProviderLike
from sleepagent.radar_agent.schemas import ContextPacket, EvidencePacket, TaskContext

from .contracts import ToolExecutionContext, ToolHandler
from .schemas import (
    AlertRuleInput,
    ExternalActionInput,
    NightSummaryInput,
    OptionalContextInput,
    QualityAssessmentInput,
    RadarReadInput,
    ReportGenerationInput,
    TrendCalculationInput,
)


def build_internal_handlers(
    *,
    provider: RadarProviderLike,
    state_writers: Mapping[str, ToolHandler] | None = None,
) -> dict[str, ToolHandler]:
    """Build handlers from existing deterministic radar services.

    External state-changing handlers remain explicit dependencies; no notification,
    export, alert, or memory write is silently mocked.
    """

    quality_gate = DataQualityGate()
    trend_agent = TrendAgent()
    report_agent = ReportAgent()
    alert_agent = AlertCareAgent()

    def radar_read(value: RadarReadInput, _: ToolExecutionContext):
        device = provider.get_device(value.radar_device_id)
        return {
            "device": device,
            "snapshots": provider.pull_snapshots(value.radar_device_id),
            "provider_night_summary": provider.pull_night_report(
                value.radar_device_id, night_of=value.night_of
            ),
        }

    def summarize(value: QualityAssessmentInput | NightSummaryInput, _: ToolExecutionContext):
        return {
            "night_summary": quality_gate.run(
                device=value.device,
                snapshots=value.snapshots,
                provider_report=value.provider_night_summary,
                night_of=value.night_of,
            )
        }

    def trend(value: TrendCalculationInput, _: ToolExecutionContext):
        return {
            "trend_result": trend_agent.analyze(
                task_id=value.task_id, summaries=value.summaries
            ).model_dump(mode="json")
        }

    def alert(value: AlertRuleInput, context: ToolExecutionContext):
        ledger = _minimal_ledger(value)
        result = alert_agent.run(_agent_context(context, ledger))
        payload = result.output_payload["alert_care"]
        return {
            "risk_level": payload["risk_level"],
            "candidate_actions": payload["candidate_actions"],
            "confirmation_actions": [
                item["action_type"] for item in payload["confirmation_requests"]
            ],
            "external_action_executed": False,
        }

    def report(value: ReportGenerationInput, context: ToolExecutionContext):
        result = report_agent.run(_agent_context(context, value.ledger))
        return {"reports": result.output_payload["reports"]}

    handlers: dict[str, ToolHandler] = {
        "radar.read": radar_read,
        "quality.assess": summarize,
        "night_summary.generate": summarize,
        "trend.calculate": trend,
        "alert_rules.evaluate": alert,
        "report.generate": report,
    }
    for source in ("weather", "room_temperature", "calendar", "medication_diet"):
        handlers[f"context.{source}.read"] = _optional_context_handler(source)
    handlers.update(state_writers or {})
    return handlers


def _optional_context_handler(source: str):
    def handler(value: OptionalContextInput, _: ToolExecutionContext):
        return {
            "source": source,
            "mode": value.mode,
            "values": value.values,
            "live_connector_used": False,
        }

    return handler


def in_memory_state_writer(status: str) -> ToolHandler:
    """Test/demo writer; production must inject a real audited service."""

    def handler(_: ExternalActionInput, __: ToolExecutionContext):
        return {"action_ref": f"action:{uuid4().hex}", "status": status}

    return handler


def _minimal_ledger(value: AlertRuleInput):
    from sleepagent.radar_agent.schemas import EvidenceLedger

    return EvidenceLedger(
        ledger_id=f"tool-ledger:{value.task_id}",
        task_id=value.task_id,
        canonical_evidence_refs=value.evidence_refs,
        derived_metrics={"risk_level": value.risk_level.value},
        confidence=1,
        caveats=["Tool evaluation is candidate-only and non-diagnostic."],
    )


def _agent_context(context: ToolExecutionContext, ledger):
    return ContextPacket(
        task_context=TaskContext(
            task_id=context.task_id,
            trace_id=context.trace_id,
            role=context.actor_role,
            purpose="orchestration",
        ),
        evidence_packet=EvidencePacket(
            evidence_refs=ledger.canonical_evidence_refs,
            evidence_ledger=ledger,
        ),
    )


__all__ = ["build_internal_handlers", "in_memory_state_writer"]
