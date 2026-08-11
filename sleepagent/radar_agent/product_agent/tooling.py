from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    InvocationOutcome,
    MultifactorSafetyInput,
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
    RUNTIME_INTERACTION_TOOLS,
    TOOL_DEFINITIONS,
    authorize_tool_invocation,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductToolExecutionContext,
    ProductToolResult,
    ToolHandler,
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


PRODUCT_TOOL_RUNTIME_VERSION = "sleepagent-product-tools.v11"

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


class ProductToolExecutor:
    """Deny-by-default execution for deterministic Product capabilities.

    Model Agents can request only registered read capabilities. The two
    questionnaire interaction commands are runtime-only state transitions;
    shared Product mutations remain reserved for the Commit Controller.
    """

    def __init__(
        self,
        handlers: dict[str, ToolHandler] | None = None,
        *,
        core_service: CoreProductToolService,
    ) -> None:
        self.core_service = core_service
        self.handlers = self.core_service.handlers()
        self._runtime_bound_outputs: dict[
            tuple[str, str, str, str], dict[str, Any]
        ] = {}
        self._runtime_bound_lock = RLock()
        self._runtime_interaction_results: dict[
            tuple[str, str, str, str], tuple[str, ProductToolResult]
        ] = {}
        self._runtime_interaction_lock = RLock()
        for tool_name, handler in (handlers or {}).items():
            self.register_handler(tool_name, handler)

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        if tool_name not in TOOL_DEFINITIONS:
            raise ProductToolError(f"unknown Product tool: {tool_name}")
        definition = TOOL_DEFINITIONS[tool_name]
        if (
            definition.effect != ToolEffect.READ_ONLY
            and tool_name not in RUNTIME_INTERACTION_TOOLS
        ):
            raise ProductToolError(
                "only read or runtime-interaction capabilities register handlers"
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
        if (
            definition.effect != ToolEffect.READ_ONLY
            and tool_name not in RUNTIME_INTERACTION_TOOLS
        ):
            raise ProductToolError(
                "state changes must use DeterministicCommitController"
            )
        if (
            tool_name in RUNTIME_INTERACTION_TOOLS
            and context.caller != "runtime"
        ):
            raise ProductToolError(
                "questionnaire interaction commands are runtime-only"
            )
        handler = self.handlers.get(tool_name)
        if handler is None:
            raise ProductToolError(f"no handler registered for {tool_name}")
        input_hash = canonical_tool_input_hash(tool_name, arguments)
        if tool_name in RUNTIME_INTERACTION_TOOLS:
            if not context.episode_id:
                raise ProductToolError(
                    "runtime interaction command requires an Episode id"
                )
            interaction_identity_hash = _runtime_interaction_identity_hash(
                tool_name,
                arguments,
            )
            interaction_payload_hash = input_hash
            interaction_key = (
                context.episode_id,
                context.fact_snapshot.fact_snapshot_hash,
                tool_name,
                interaction_identity_hash,
            )
            idempotency_key = canonical_runtime_interaction_idempotency_key(
                tool_name,
                arguments,
                episode_id=context.episode_id,
                fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            )
            with self._runtime_interaction_lock:
                cached = self._runtime_interaction_results.get(interaction_key)
                if cached is not None:
                    if cached[0] != interaction_payload_hash:
                        return self._runtime_interaction_conflict(
                            tool_name,
                            context=context,
                            input_hash=interaction_payload_hash,
                            idempotency_key=idempotency_key,
                        )
                    if not _runtime_interaction_cache_expired(
                        tool_name,
                        cached[1],
                        arguments=arguments,
                    ):
                        return cached[1].model_copy(deep=True)
                    del self._runtime_interaction_results[interaction_key]
                result = self._execute_once(
                    tool_name,
                    arguments,
                    context=context,
                    input_hash=interaction_payload_hash,
                    idempotency_key=idempotency_key,
                )
                if result.receipt.outcome == InvocationOutcome.SUCCEEDED:
                    self._runtime_interaction_results[interaction_key] = (
                        interaction_payload_hash,
                        result.model_copy(deep=True),
                    )
                return result
        return self._execute_once(
            tool_name,
            arguments,
            context=context,
            input_hash=input_hash,
            idempotency_key=None,
        )

    def _execute_once(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ProductToolExecutionContext,
        input_hash: str,
        idempotency_key: str | None,
    ) -> ProductToolResult:
        definition = TOOL_DEFINITIONS[tool_name]
        handler = self.handlers[tool_name]
        try:
            if (
                isinstance(context.caller, AgentId)
                and tool_name in _RUNTIME_BOUND_SELECTOR_TOOLS
            ):
                if not context.episode_id:
                    raise ProductToolError(
                        "Agent Tool selector requires an Episode binding"
                    )
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
                    context.episode_id,
                    context.fact_snapshot.fact_snapshot_hash,
                    tool_name,
                    bound_invocation_id,
                )
                with self._runtime_bound_lock:
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
            tool_invocation_id=canonical_tool_invocation_id(
                tool_name,
                input_hash,
            ),
            tool_name=tool_name,
            tool_version=f"{tool_name}.{definition.version}",
            caller=(
                context.caller.value
                if isinstance(context.caller, AgentId)
                else context.caller
            ),
            fact_snapshot_id=context.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            input_hash=input_hash,
            effect=definition.effect,
            outcome=outcome,
            observed_at=datetime.now(timezone.utc),
            output=output,
            source_refs=list(output.get("source_refs", [])),
            idempotency_key=idempotency_key,
            error_code=error_code,
        )
        if (
            receipt.outcome == InvocationOutcome.SUCCEEDED
            and context.caller == "runtime"
            and tool_name in _RUNTIME_BOUND_SELECTOR_TOOLS
        ):
            if not context.episode_id:
                raise ProductToolError(
                    "runtime Tool binding requires an Episode id"
                )
            cache_key = (
                context.episode_id,
                context.fact_snapshot.fact_snapshot_hash,
                tool_name,
                receipt.tool_invocation_id,
            )
            with self._runtime_bound_lock:
                self._runtime_bound_outputs[cache_key] = deepcopy(receipt.output)
        item = (
            context_item_from_tool_receipt(receipt)
            if outcome == InvocationOutcome.SUCCEEDED
            else None
        )
        return ProductToolResult(receipt=receipt, context_item=item)

    @staticmethod
    def _runtime_interaction_conflict(
        tool_name: str,
        *,
        context: ProductToolExecutionContext,
        input_hash: str,
        idempotency_key: str,
    ) -> ProductToolResult:
        definition = TOOL_DEFINITIONS[tool_name]
        receipt = ToolReceipt(
            tool_invocation_id=canonical_tool_invocation_id(
                tool_name,
                input_hash,
            ),
            tool_name=tool_name,
            tool_version=f"{tool_name}.{definition.version}",
            caller="runtime",
            fact_snapshot_id=context.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            input_hash=input_hash,
            effect=definition.effect,
            outcome=InvocationOutcome.FAILED,
            observed_at=datetime.now(timezone.utc),
            output={},
            idempotency_key=idempotency_key,
            error_code="RuntimeInteractionPayloadConflict",
        )
        return ProductToolResult(receipt=receipt, context_item=None)

    def release_episode(self, episode_id: str) -> None:
        """Release transient Tool outputs after an Episode becomes terminal."""

        if not episode_id:
            raise ValueError("episode_id is required")
        with self._runtime_bound_lock:
            keys = [
                key for key in self._runtime_bound_outputs if key[0] == episode_id
            ]
            for key in keys:
                del self._runtime_bound_outputs[key]
        with self._runtime_interaction_lock:
            interaction_keys = [
                key
                for key in self._runtime_interaction_results
                if key[0] == episode_id
            ]
            for key in interaction_keys:
                del self._runtime_interaction_results[key]

    def runtime_binding_count(self, episode_id: str) -> int:
        """Expose a bounded diagnostic for cache lifecycle tests."""

        with self._runtime_bound_lock:
            return sum(
                1 for key in self._runtime_bound_outputs if key[0] == episode_id
            )


class CoreProductToolService:
    """Real deterministic owners for stateless Product read capabilities."""

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
            "artifact.render": self._render,
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
        if "night_summaries" not in arguments:
            raise ValueError(
                "trend calculation requires structured night_summaries"
            )
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
        raise ValueError(
            "artifact.render requires a FactSnapshot-bound typed request"
        )

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


def _runtime_interaction_identity_hash(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    if tool_name == "questionnaire.select_profile":
        request = arguments.get("request")
        if isinstance(request, dict):
            material = {
                key: request.get(key)
                for key in (
                    "request_id",
                    "episode_id",
                    "subject_id",
                    "actor_id",
                    "role",
                    "plan_id",
                    "plan_revision",
                    "plan_step_id",
                )
            }
        else:
            material = {"request": request}
    elif tool_name == "questionnaire.capture_profile":
        selection = arguments.get("selection")
        if isinstance(selection, dict):
            material = {
                key: selection.get(key)
                for key in (
                    "selection_id",
                    "episode_id",
                    "subject_id",
                    "actor_id",
                    "role",
                )
            }
        else:
            material = {"selection": selection}
    else:  # pragma: no cover - guarded by the frozen interaction allowlist.
        material = arguments
    return stable_hash({"tool_name": tool_name, "identity": material})


def _runtime_interaction_payload_hash(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    material = deepcopy(arguments)
    material.pop("_now", None)
    if tool_name == "questionnaire.select_profile":
        request = material.get("request")
        if isinstance(request, dict):
            request.pop("remaining_episode_budget", None)
    return stable_hash(material)


def canonical_tool_input_hash(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Hash the exact payload material owned by the Product Tool runtime."""

    if tool_name in RUNTIME_INTERACTION_TOOLS:
        return _runtime_interaction_payload_hash(tool_name, arguments)
    return stable_hash(arguments)


def canonical_tool_invocation_id(tool_name: str, input_hash: str) -> str:
    return f"tool:{tool_name}:{input_hash[:16]}"


def canonical_runtime_interaction_idempotency_key(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    episode_id: str,
    fact_snapshot_hash: str,
) -> str:
    if tool_name not in RUNTIME_INTERACTION_TOOLS:
        raise ValueError("Tool is not a runtime interaction")
    return "runtime-interaction:" + stable_hash(
        {
            "episode_id": episode_id,
            "fact_snapshot_hash": fact_snapshot_hash,
            "tool_name": tool_name,
            "interaction_identity_hash": _runtime_interaction_identity_hash(
                tool_name,
                arguments,
            ),
        }
    )


def _runtime_interaction_cache_expired(
    tool_name: str,
    result: ProductToolResult,
    *,
    arguments: dict[str, Any],
) -> bool:
    if tool_name != "questionnaire.capture_profile":
        return False
    capture = result.receipt.output.get("capture")
    if not isinstance(capture, dict):
        return True
    try:
        read_at = _runtime_interaction_read_at(arguments)
        expiries = [
            item["episode_valid_until"]
            for item in capture.get("answers", ())
        ] + [
            item["valid_until"]
            for item in capture.get("safety_events", ())
        ]
        return any(_parse_runtime_time(item) <= read_at for item in expiries)
    except (KeyError, TypeError, ValueError):
        return True


def _runtime_interaction_read_at(arguments: dict[str, Any]) -> datetime:
    value = arguments.get("_now")
    if value is None:
        return datetime.now(timezone.utc)
    return _parse_runtime_time(value)


def _parse_runtime_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("runtime interaction time must be datetime or ISO string")
    if parsed.tzinfo is None:
        raise ValueError("runtime interaction time must be timezone-aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "CoreProductToolService",
    "PRODUCT_TOOL_RUNTIME_VERSION",
    "ProductToolError",
    "ProductToolExecutionContext",
    "ProductToolExecutor",
    "ProductToolResult",
    "ToolHandler",
    "canonical_runtime_interaction_idempotency_key",
    "canonical_tool_input_hash",
    "canonical_tool_invocation_id",
    "context_item_from_tool_receipt",
]
