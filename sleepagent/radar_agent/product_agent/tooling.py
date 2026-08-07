from __future__ import annotations

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


PRODUCT_TOOL_RUNTIME_VERSION = "sleepagent-product-tools.v8"


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
            "radar.assess_data_quality": self._quality,
            "radar.get_device_status": self._passthrough,
            "radar.get_night_evidence": self._passthrough,
            "radar.get_range_evidence": self._passthrough,
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
        values = arguments.get("values", [])
        if not isinstance(values, list) or not all(
            isinstance(item, (int, float)) for item in values
        ):
            raise ValueError("trend values must be numeric")
        if not values:
            return {
                "count": 0,
                "mean": None,
                "change": None,
                "source_refs": list(arguments.get("source_refs", [])),
            }
        return {
            "count": len(values),
            "mean": mean(values),
            "change": values[-1] - values[0] if len(values) > 1 else 0,
            "source_refs": list(arguments.get("source_refs", [])),
        }

    @staticmethod
    def _urgent(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        text = str(arguments.get("text", "")).lower()
        phrases = ("呼吸困难", "胸痛", "昏厥", "cannot breathe", "chest pain")
        return {
            "urgent": any(item in text for item in phrases),
            "matched_policy": "urgent-boundary.v1",
            "source_refs": [],
        }

    @staticmethod
    def _risk(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if "safety_factors" in arguments:
            decision = fuse_multifactor_safety(
                MultifactorSafetyInput.model_validate(
                    arguments["safety_factors"]
                )
            )
            return decision.model_dump(mode="json")
        score = float(arguments.get("score", 0))
        level = "normal" if score < 0.4 else "watch" if score < 0.8 else "escalate"
        return {
            "risk_level": level,
            "source_refs": list(arguments.get("source_refs", [])),
        }

    @staticmethod
    def _quality(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        coverage = float(arguments.get("coverage_ratio", 0))
        return {
            "coverage_ratio": coverage,
            "usable": coverage >= 0.6,
            "source_refs": list(arguments.get("source_refs", [])),
        }

    @staticmethod
    def _reviewed_knowledge(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        if not arguments.get("reviewed", False):
            raise ValueError("knowledge retrieval must be reviewed-only")
        return {
            "passages": list(arguments.get("passages", [])),
            "citations": list(arguments.get("citations", [])),
            "source_refs": list(arguments.get("citations", [])),
        }

    @staticmethod
    def _render(
        arguments: dict[str, Any], context: ProductToolExecutionContext
    ) -> dict[str, Any]:
        return {
            "artifact_bytes_hash": stable_hash(arguments.get("content", "")),
            "rendered": True,
            "committed": False,
            "source_refs": list(arguments.get("source_refs", [])),
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
