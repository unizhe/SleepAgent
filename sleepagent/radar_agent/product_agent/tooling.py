from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Callable, Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    FactSnapshot,
    InvocationOutcome,
    MultifactorSafetyInput,
    StrictContract,
    ToolEffect,
    ToolReceipt,
    TrustLabel,
    TrustedContextItem,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.online_reasoning import (
    fuse_multifactor_safety,
)
from sleepagent.radar_agent.product_agent.registry import (
    TOOL_DEFINITIONS,
    authorize_tool_invocation,
)
from sleepagent.radar_agent.product_agent.policies.risk import (
    RISK_POLICY_VERSION,
    match_urgent_boundary,
)
from sleepagent.radar_agent.product_agent.services.reviewed_knowledge import (
    ReviewedKnowledgeQuery,
)
from sleepagent.radar_agent.product_agent.tools.artifact_rendering import (
    ArtifactRenderRequest,
    ArtifactRenderingTool,
    ProductArtifactBasisRequest,
)
from sleepagent.radar_agent.product_agent.tools.knowledge_retrieval import (
    KnowledgeRetrievalTool,
)
from sleepagent.radar_agent.product_agent.tools.risk_classification import (
    DeterministicRiskSnapshot,
    RiskClassificationInput,
    RiskClassificationTool,
    RiskObservation,
    TrendRiskSignal,
)
from sleepagent.radar_agent.product_agent.tools.trend_analysis import (
    TrendAnalysisTool,
)
from sleepagent.radar_agent.product_agent.tools.radar_data import (
    CanonicalRadarEvidenceTool,
)
from sleepagent.radar_agent.schemas import RadarNightSummary


PRODUCT_TOOL_RUNTIME_VERSION = "sleepagent-product-tools.v8"

_RUNTIME_BOUND_SELECTOR_TOOLS = frozenset(
    {
        "radar.get_night_evidence",
        "radar.get_range_evidence",
        "radar.assess_data_quality",
        "radar.get_device_status",
        "trend.calculate_metrics",
        "risk.classify_signal",
        "artifact.render",
        "coordination.read_policy",
    }
)


class ProductToolError(RuntimeError):
    pass


class ProductToolExecutionContext(StrictContract):
    caller: AgentId | str
    fact_snapshot: FactSnapshot
    authorization_scope: tuple[str, ...] = ()
    episode_id: str | None = None
    plan_id: str | None = None
    plan_revision: int | None = Field(default=None, ge=0)
    allowed_plan_step_ids: tuple[str, ...] = ()
    plan_step_id: str | None = None
    invocation_id: str = Field(default="runtime:unbound", min_length=1)
    user_intent_ref: str | None = None
    user_intent_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    user_intent_purpose: Literal[
        "explicit_memory_review",
        "explicit_memory_change",
        "explicit_memory_forget",
    ] | None = None
    max_memory_items: int = Field(default=8, ge=1, le=20)
    memory_token_budget: int = Field(default=1200, ge=64, le=4000)


class ProductToolResult(StrictContract):
    receipt: ToolReceipt
    context_item: TrustedContextItem | None = None


ToolHandler = Callable[[dict[str, Any], ProductToolExecutionContext], dict[str, Any]]


class ProductToolExecutor:
    """Deny-by-default execution for deterministic tools.

    Model Agents can request only registered read capabilities. Mutations remain
    reserved for the deterministic Commit Controller.
    """

    def __init__(self, handlers: dict[str, ToolHandler] | None = None) -> None:
        self.handlers = ExistingCapabilityToolHandlers().handlers()
        self._runtime_bound_outputs: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}
        for tool_name, handler in (handlers or {}).items():
            self.register_handler(tool_name, handler)

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        if tool_name not in TOOL_DEFINITIONS:
            raise ProductToolError(f"unknown Product tool: {tool_name}")
        if TOOL_DEFINITIONS[tool_name].effect != ToolEffect.READ_ONLY:
            raise ProductToolError("only read capabilities can register handlers")
        if tool_name == "memory.read":
            from sleepagent.radar_agent.product_agent.longitudinal_memory import (
                LongitudinalMemoryService,
            )

            if not isinstance(
                getattr(handler, "__self__", None),
                LongitudinalMemoryService,
            ):
                raise ProductToolError(
                    "memory.read must use LongitudinalMemoryService.read"
                )
        self.handlers[tool_name] = handler

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ProductToolExecutionContext,
    ) -> ProductToolResult:
        authorize_tool_invocation(context.caller, tool_name)
        definition = TOOL_DEFINITIONS[tool_name]
        if definition.effect != ToolEffect.READ_ONLY:
            raise ProductToolError(
                "state changes must use DeterministicCommitController"
            )
        handler = self.handlers.get(tool_name)
        if handler is None:
            raise ProductToolError(f"no handler registered for {tool_name}")
        input_hash = stable_hash(arguments)
        try:
            if (
                isinstance(context.caller, AgentId)
                and tool_name in _RUNTIME_BOUND_SELECTOR_TOOLS
            ):
                if set(arguments) != {"bound_tool_invocation_id"}:
                    raise PermissionError(
                        "Agent Tool requests require one runtime-bound selector"
                    )
                bound_invocation_id = arguments["bound_tool_invocation_id"]
                if not isinstance(bound_invocation_id, str):
                    raise ValueError(
                        "bound_tool_invocation_id must be a string"
                    )
                cache_key = (
                    context.fact_snapshot.fact_snapshot_hash,
                    tool_name,
                    bound_invocation_id,
                )
                cached = self._runtime_bound_outputs.get(cache_key)
                if cached is None:
                    raise ProductToolError(
                        "runtime has not bound this Tool output to FactSnapshot"
                    )
                output = deepcopy(cached)
            else:
                output = handler(arguments, context)
            outcome = InvocationOutcome.SUCCEEDED
            error_code = None
        except Exception as exc:
            output = {}
            outcome = InvocationOutcome.FAILED
            error_code = type(exc).__name__
        receipt = ToolReceipt(
            tool_invocation_id=f"tool:{tool_name}:{input_hash[:16]}",
            tool_name=tool_name,
            tool_version=f"{tool_name}.v1",
            caller=(
                context.caller.value
                if isinstance(context.caller, AgentId)
                else context.caller
            ),
            fact_snapshot_id=context.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            input_hash=input_hash,
            effect=ToolEffect.READ_ONLY,
            outcome=outcome,
            observed_at=datetime.now(timezone.utc),
            output=output,
            source_refs=list(output.get("source_refs", [])),
            error_code=error_code,
        )
        if (
            receipt.outcome == InvocationOutcome.SUCCEEDED
            and context.caller == "runtime"
            and tool_name in _RUNTIME_BOUND_SELECTOR_TOOLS
        ):
            cache_key = (
                context.fact_snapshot.fact_snapshot_hash,
                tool_name,
                receipt.tool_invocation_id,
            )
            self._runtime_bound_outputs[cache_key] = deepcopy(receipt.output)
        item = (
            context_item_from_tool_receipt(receipt)
            if outcome == InvocationOutcome.SUCCEEDED
            else None
        )
        return ProductToolResult(receipt=receipt, context_item=item)


class ExistingCapabilityToolHandlers:
    """Adapters for capabilities formerly misclassified as child Agents."""

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            "runtime.build_fact_snapshot": self._snapshot,
            "trend.calculate_metrics": self._trend,
            "risk.match_urgent_boundary": self._urgent,
            "risk.classify_signal": self._risk,
            "radar.assess_data_quality": CanonicalRadarEvidenceTool.assess_quality,
            "radar.get_device_status": CanonicalRadarEvidenceTool.read_device_status,
            "radar.get_night_evidence": CanonicalRadarEvidenceTool.read,
            "radar.get_range_evidence": CanonicalRadarEvidenceTool.read,
            "knowledge.retrieve_reviewed": self._reviewed_knowledge,
            "evidence.read_ledger": self._passthrough,
            "care.read_state": self._passthrough,
            "care.read_catalog": self._passthrough,
            "care.read_constraints": self._passthrough,
            "care.read_feedback": self._passthrough,
            "questionnaire.select": self._passthrough,
            "artifact.read": self._passthrough,
            "artifact.render": self._render,
            "memory.compare": self._memory_compare,
            "coordination.read_policy": self._passthrough,
            "coordination.read_schedule": self._passthrough,
            "device.read_delivery_policy": self._passthrough,
            "policy.read": self._passthrough,
            "confirmation.validate": self._passthrough,
        }

    @staticmethod
    def _passthrough(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        return {
            "data": arguments.get("data", {}),
            "source_refs": list(arguments.get("source_refs", [])),
        }

    @staticmethod
    def _snapshot(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        return {
            "fact_snapshot_id": context.fact_snapshot.fact_snapshot_id,
            "fact_snapshot_hash": context.fact_snapshot.fact_snapshot_hash,
            "source_refs": list(context.fact_snapshot.source_refs),
        }

    @staticmethod
    def _trend(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        caller = (
            context.caller.value
            if hasattr(context.caller, "value")
            else str(context.caller)
        )
        if caller != "runtime":
            raise PermissionError(
                "trend facts must be injected by runtime"
            )
        if "night_summaries" in arguments:
            raw_summaries = arguments["night_summaries"]
            if not isinstance(raw_summaries, list):
                raise ValueError("night_summaries must be a list")
            summaries = [
                item
                if isinstance(item, RadarNightSummary)
                else RadarNightSummary.model_validate(item)
                for item in raw_summaries
            ]
            binding_subject = context.fact_snapshot.binding.subject_id
            authorized_refs = set(context.fact_snapshot.source_refs)
            scope = context.fact_snapshot.source_scope
            for summary in summaries:
                if not _subject_matches_snapshot(
                    canonical_subject=summary.subject_id or "",
                    binding_subject=binding_subject,
                ):
                    raise ValueError(
                        "night summary subject does not match FactSnapshot"
                    )
                if (
                    scope.date_start is None
                    or scope.date_end is None
                    or not scope.date_start <= summary.night_of <= scope.date_end
                ):
                    raise ValueError(
                        "night summary falls outside FactSnapshot source scope"
                    )
                summary_ref = summary.source_report_ref or (
                    f"night-summary:{summary.radar_device_id}:"
                    f"{summary.night_of.isoformat()}"
                )
                if summary_ref not in authorized_refs:
                    raise ValueError(
                        "night summary source is absent from FactSnapshot"
                    )
            output = TrendAnalysisTool().analyze(summaries).model_dump(mode="json")
            all_refs = list(output.get("source_refs", []))
            output["analysis_source_refs"] = all_refs
            output["source_ref_count"] = len(all_refs)
            output["source_refs"] = all_refs[:50]
            return output
        values = arguments.get("values", [])
        scalar_refs = [str(item) for item in arguments.get("source_refs", [])]
        if not set(scalar_refs).issubset(
            set(context.fact_snapshot.source_refs)
        ):
            raise ValueError("trend refs exceed FactSnapshot scope")
        if not isinstance(values, list) or not all(
            isinstance(item, (int, float)) for item in values
        ):
            raise ValueError("trend values must be numeric")
        if not values:
            return {
                "count": 0,
                "mean": None,
                "change": None,
                "source_refs": scalar_refs,
            }
        return {
            "count": len(values),
            "mean": mean(values),
            "change": values[-1] - values[0] if len(values) > 1 else 0,
            "source_refs": scalar_refs,
        }
    @staticmethod
    def _urgent(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError("urgent facts must be injected by runtime")
        raw_inputs = arguments.get("text_inputs")
        if raw_inputs is None:
            text_inputs = (str(arguments.get("text", "")),)
        elif isinstance(raw_inputs, (list, tuple)) and all(
            isinstance(item, str) for item in raw_inputs
        ):
            text_inputs = tuple(raw_inputs)
        else:
            raise ValueError("urgent boundary text_inputs must be strings")
        match = match_urgent_boundary(text_inputs)
        return {
            "urgent": match is not None,
            "matched_policy": RISK_POLICY_VERSION,
            "matched_term": match.matched_term if match is not None else None,
            "source_refs": [match.evidence_ref] if match is not None else [],
        }

    @staticmethod
    def _risk(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError("risk facts must be injected by runtime")
        if "safety_factors" in arguments:
            factors = MultifactorSafetyInput.model_validate(
                arguments["safety_factors"]
            )
            result = RiskClassificationTool().classify(
                RiskClassificationInput(safety_factors=factors)
            ).model_dump(mode="json")
            # Runtime consumers already use this non-authoritative explanation
            # hint. Keep it at the adapter edge while Risk Tool output remains
            # limited to classification facts.
            result["personalization_effect"] = fuse_multifactor_safety(
                factors
            ).personalization_effect
            return result
        data = arguments.get("data")
        if isinstance(data, dict) and data.get("risk_state"):
            source_refs = tuple(
                str(item) for item in arguments.get("source_refs", ())
            )
            if not source_refs or not set(source_refs).issubset(
                set(context.fact_snapshot.source_refs)
            ):
                raise ValueError(
                    "deterministic risk refs must be bound to FactSnapshot"
                )
            snapshot = DeterministicRiskSnapshot(
                risk_state=data["risk_state"],
                data_sufficiency=data.get("data_sufficiency", "unknown"),
                health_escalation_allowed=bool(
                    data.get("health_escalation_allowed", False)
                ),
                reason_codes=tuple(data.get("reason_codes", ())),
                source_refs=source_refs,
            )
            return RiskClassificationTool().classify(
                RiskClassificationInput(deterministic_snapshot=snapshot)
            ).model_dump(mode="json")
        if "observation" in arguments or "trend_signals" in arguments:
            observation = (
                RiskObservation.model_validate(arguments["observation"])
                if arguments.get("observation") is not None
                else None
            )
            trend_signals = tuple(
                TrendRiskSignal.model_validate(item)
                for item in arguments.get("trend_signals", ())
            )
            structured_refs = {
                *(
                    observation.source_refs
                    if observation is not None
                    else ()
                ),
                *(
                    ref
                    for signal in trend_signals
                    for ref in signal.source_refs
                ),
            }
            if not structured_refs.issubset(
                set(context.fact_snapshot.source_refs)
            ):
                raise ValueError(
                    "structured risk refs exceed FactSnapshot scope"
                )
            return RiskClassificationTool().classify(
                RiskClassificationInput(
                    text_inputs=tuple(arguments.get("text_inputs", ())),
                    observation=observation,
                    trend_signals=trend_signals,
                )
            ).model_dump(mode="json")
        return RiskClassificationTool().classify(
            RiskClassificationInput(
                text_inputs=tuple(arguments.get("text_inputs", ())),
            )
        ).model_dump(mode="json")

    @staticmethod
    def _reviewed_knowledge(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if "reviewed" in arguments:
            raise ValueError("review status is repository policy, not caller input")
        if "subject_id" in arguments:
            raise ValueError("knowledge subject is derived from FactSnapshot")
        roles = arguments.get("roles") or (context.fact_snapshot.binding.role,)
        roles = tuple(roles)
        scopes = {
            *context.authorization_scope,
            *context.fact_snapshot.binding.authorization_scope,
        }
        binding_role = context.fact_snapshot.binding.role
        allowed_roles = {binding_role}
        if "draft_material" in scopes:
            allowed_roles.add("doctor")
        if binding_role == "system" and "internal_agent_analysis" in scopes:
            allowed_roles.update({"elder", "family", "doctor", "system"})
        if not set(roles).issubset(allowed_roles):
            raise PermissionError(
                "knowledge roles exceed FactSnapshot authorization"
            )
        request = ReviewedKnowledgeQuery(
            query=str(arguments.get("query", "")),
            roles=roles,
            subject_id=context.fact_snapshot.binding.subject_id,
            limit=int(arguments.get("limit", 8)),
        )
        return KnowledgeRetrievalTool().retrieve(request).model_dump(mode="json")

    @staticmethod
    def _render(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if {
            "context",
            "evidence_ledger",
            "audience_role",
        }.issubset(arguments):
            if _caller_name(context) != "runtime":
                raise PermissionError(
                    "typed artifact facts must be injected by runtime"
                )
            request = ArtifactRenderRequest.model_validate(arguments)
            snapshot = context.fact_snapshot
            if not context.episode_id:
                raise ValueError(
                    "typed artifact rendering requires a Product Episode binding"
                )
            if request.context.task_context.task_id != context.episode_id:
                raise ValueError(
                    "artifact task identity does not match Product Episode"
                )
            if request.context.task_context.role != snapshot.binding.role:
                raise PermissionError(
                    "artifact Context role does not match FactSnapshot binding"
                )
            ledger_refs = set(request.evidence_ledger.canonical_evidence_refs)
            if not ledger_refs:
                raise ValueError(
                    "artifact EvidenceLedger requires canonical evidence"
                )
            if not ledger_refs.issubset(set(snapshot.source_refs)):
                raise ValueError(
                    "artifact EvidenceLedger is outside FactSnapshot sources"
                )
            packet = request.context.evidence_packet
            if packet.vital_snapshots or packet.supplementary_documents:
                raise ValueError(
                    "typed artifact bridge cannot bind unreferenced vital or "
                    "supplementary payloads"
                )
            if not set(packet.evidence_refs).issubset(
                set(snapshot.source_refs)
            ):
                raise ValueError(
                    "artifact Context evidence exceeds FactSnapshot sources"
                )
            scope = snapshot.source_scope
            for summary in packet.night_summaries:
                if not _subject_matches_snapshot(
                    canonical_subject=summary.subject_id or "",
                    binding_subject=snapshot.binding.subject_id,
                ):
                    raise ValueError(
                        "artifact night summary subject does not match FactSnapshot"
                    )
                if (
                    scope.date_start is None
                    or scope.date_end is None
                    or not scope.date_start <= summary.night_of <= scope.date_end
                ):
                    raise ValueError(
                        "artifact night summary exceeds FactSnapshot date scope"
                    )
                summary_ref = summary.source_report_ref or ""
                if summary_ref not in snapshot.source_refs:
                    raise ValueError(
                        "artifact night summary source is not authorized"
                    )
                if summary.source_raw_event_ids:
                    raise ValueError(
                        "artifact Context cannot contain raw event references"
                    )
            questionnaires = [
                *packet.questionnaire_entries,
                *request.evidence_ledger.questionnaire_entries,
            ]
            for entry in questionnaires:
                if not _subject_matches_snapshot(
                    canonical_subject=entry.subject_id,
                    binding_subject=snapshot.binding.subject_id,
                ):
                    raise ValueError(
                        "artifact questionnaire subject does not match FactSnapshot"
                    )
                if (
                    not entry.evidence_ref
                    or entry.evidence_ref not in snapshot.source_refs
                ):
                    raise ValueError(
                        "artifact questionnaire source is not authorized"
                    )
            scopes = {
                *context.authorization_scope,
                *snapshot.binding.authorization_scope,
            }
            if (
                request.audience_role != snapshot.binding.role
                and "draft_material" not in scopes
            ):
                raise PermissionError(
                    "cross-role artifact rendering requires draft_material"
                )
            if (
                request.audience_role == "doctor"
                and "draft_material" not in scopes
            ):
                raise PermissionError(
                    "doctor material rendering requires draft_material"
                )
            return ArtifactRenderingTool().render(request).model_dump(mode="json")
        if {
            "episode_id",
            "accepted_evidence_ref",
            "accepted_evidence_hash",
            "evidence_packet",
            "audience_role",
        }.issubset(arguments):
            if _caller_name(context) != "runtime":
                raise PermissionError(
                    "accepted artifact basis must be injected by runtime"
                )
            request = ProductArtifactBasisRequest.model_validate(arguments)
            if not context.episode_id or request.episode_id != context.episode_id:
                raise ValueError(
                    "artifact basis does not match Product Episode"
                )
            if request.evidence_packet.source_scope != context.fact_snapshot.source_scope:
                raise ValueError(
                    "artifact basis Evidence expands FactSnapshot SourceScope"
                )
            snapshot = context.fact_snapshot
            scopes = {
                *context.authorization_scope,
                *snapshot.binding.authorization_scope,
            }
            if (
                request.audience_role != snapshot.binding.role
                and "draft_material" not in scopes
            ):
                raise PermissionError(
                    "cross-role artifact basis requires draft_material"
                )
            if (
                request.audience_role == "doctor"
                and "draft_material" not in scopes
            ):
                raise PermissionError(
                    "doctor material basis requires draft_material"
                )
            return ArtifactRenderingTool().prepare_product_basis(
                request
            ).model_dump(mode="json")
        return {
            "artifact_bytes_hash": stable_hash(arguments.get("content", "")),
            "rendered": True,
            "committed": False,
            "compatibility_mode": "content_hash_only",
            "source_refs": [],
        }

    @staticmethod
    def _memory_compare(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        current = arguments.get("current")
        candidate = arguments.get("candidate")
        return {
            "same": current == candidate,
            "semantic_conflict_candidate": (
                current is not None and candidate is not None and current != candidate
            ),
            "source_refs": list(arguments.get("source_refs", [])),
        }


def _subject_matches_snapshot(
    *,
    canonical_subject: str,
    binding_subject: str,
) -> bool:
    if not canonical_subject:
        return False
    if canonical_subject == binding_subject:
        return True
    marker = "::subject::"
    return marker in binding_subject and (
        binding_subject.split(marker, 1)[1] == canonical_subject
    )


def _caller_name(context: ProductToolExecutionContext) -> str:
    caller = context.caller
    return caller.value if hasattr(caller, "value") else str(caller)


def context_item_from_tool_receipt(receipt: ToolReceipt) -> TrustedContextItem:
    trust_label = (
        TrustLabel.USER_DATA
        if receipt.tool_name
        in {
            "profile.read",
            "profile.build_change_set",
            "questionnaire.capture_profile",
        }
        else TrustLabel.TOOL_OUTPUT_UNTRUSTED
    )
    return TrustedContextItem(
        key=f"tool_receipt:{receipt.tool_name}",
        trust_label=trust_label,
        value={
            "tool_invocation_id": receipt.tool_invocation_id,
            "tool_name": receipt.tool_name,
            "outcome": receipt.outcome.value,
            "output": receipt.output,
        },
        source_refs=tuple([receipt.tool_invocation_id, *receipt.source_refs]),
    )


__all__ = [
    "PRODUCT_TOOL_RUNTIME_VERSION",
    "ExistingCapabilityToolHandlers",
    "ProductToolError",
    "ProductToolExecutionContext",
    "ProductToolExecutor",
    "ProductToolResult",
    "context_item_from_tool_receipt",
]
