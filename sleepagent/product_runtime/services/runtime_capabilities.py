from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sleepagent.product_runtime.contracts import AgentId
from sleepagent.product_runtime.governance import (
    GOVERNANCE_VERSION,
    PRODUCT_SAFETY_POLICY_VERSION,
    CareActionCatalog,
    CareContextState,
)
from sleepagent.product_runtime.policies.workflow import (
    CANONICAL_WORKFLOW_POLICY,
)
from sleepagent.product_runtime.registry import REGISTRY_VERSION
from sleepagent.product_runtime.runtime_ports import (
    ProductToolExecutionContext,
)
from sleepagent.product_runtime.tools.care_coordination import (
    CareCoordinationPolicyRequest,
    CareCoordinationTool,
)


PRODUCT_RUNTIME_READ_SERVICE_VERSION = "sleepagent-runtime-read-service.v1"


class CareContextReader(Protocol):
    def get(self, subject_id: str) -> CareContextState: ...


@dataclass(frozen=True, slots=True)
class ProductRuntimeReadService:
    """Typed owner for Product policy and Care-state read capabilities."""

    care_store: CareContextReader
    care_catalog: CareActionCatalog

    def read_current_care_state(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        _require_no_arguments(arguments, tool_name="care.read_state")
        state = self.care_store.get(
            context.fact_snapshot.binding.subject_id
        )
        return {
            "state": state.model_dump(mode="json"),
            "source_refs": [f"care-state:{state.subject_id}:v{state.version}"],
        }

    def read_care_catalog(
        self,
        arguments: dict[str, Any],
        _context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        _require_no_arguments(arguments, tool_name="care.read_catalog")
        definitions = self.care_catalog.list_definitions()
        return {
            "catalog_version": GOVERNANCE_VERSION,
            "actions": [item.model_dump(mode="json") for item in definitions],
            "source_refs": [
                f"care-catalog:{item.care_action_id}:v{item.version}"
                for item in definitions
            ],
        }

    def read_care_constraints(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        unknown = set(arguments) - {"care_action_ids"}
        if unknown:
            raise ValueError(
                "care.read_constraints received unsupported arguments"
            )
        requested = arguments.get("care_action_ids", ())
        if not isinstance(requested, (list, tuple)) or not all(
            isinstance(item, str) and item for item in requested
        ):
            raise ValueError("care_action_ids must be non-empty strings")
        requested_ids = set(requested)
        definitions = tuple(
            item
            for item in self.care_catalog.list_definitions()
            if not requested_ids or item.care_action_id in requested_ids
        )
        missing = requested_ids - {
            item.care_action_id for item in definitions
        }
        if missing:
            raise ValueError("unknown Care catalog action requested")
        active_codes = tuple(
            dict.fromkeys(context.fact_snapshot.active_constraint_codes)
        )
        return {
            "catalog_version": GOVERNANCE_VERSION,
            "active_constraint_codes": list(active_codes),
            "constraints": [
                {
                    "care_action_id": item.care_action_id,
                    "version": item.version,
                    "allowed_parameters": item.allowed_parameters,
                    "contraindication_codes": item.contraindication_codes,
                    "active_contraindication_codes": [
                        code
                        for code in item.contraindication_codes
                        if code in active_codes
                    ],
                    "delivery_required": item.delivery_required,
                    "allowed_delivery_timings": [
                        value.value for value in item.allowed_delivery_timings
                    ],
                    "allowed_delivery_modalities": [
                        value.value for value in item.allowed_delivery_modalities
                    ],
                }
                for item in definitions
            ],
            "source_refs": [
                context.fact_snapshot.fact_snapshot_id,
                *(
                    f"care-catalog:{item.care_action_id}:v{item.version}"
                    for item in definitions
                ),
            ],
        }

    def read_delivery_policy(
        self,
        arguments: dict[str, Any],
        _context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        _require_no_arguments(
            arguments,
            tool_name="device.read_delivery_policy",
        )
        policy = self.care_catalog.delivery_policy
        return {
            "policy": policy.model_dump(mode="json"),
            "source_refs": [policy.device_policy_ref, policy.policy_ref],
        }

    def read_coordination_policy(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        caller = (
            context.caller.value
            if isinstance(context.caller, AgentId)
            else context.caller
        )
        if caller != "runtime":
            raise PermissionError(
                "coordination policy facts must be injected by runtime"
            )
        policy = self.care_catalog.delivery_policy
        if "coordination_policy_ref" in arguments:
            raise ValueError(
                "coordination policy identity is derived from the Care catalog"
            )
        request = CareCoordinationPolicyRequest.model_validate(
            {
                **arguments,
                "coordination_policy_ref": policy.coordination_policy_ref,
            }
        )
        result = CareCoordinationTool().read_policy(request)
        return {
            **result.model_dump(mode="json"),
            "policy": {
                "coordination_policy_ref": policy.coordination_policy_ref,
                "family_notification_requires_candidate": True,
            },
        }

    def read_runtime_policy(
        self,
        arguments: dict[str, Any],
        _context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        _require_no_arguments(arguments, tool_name="policy.read")
        workflow = CANONICAL_WORKFLOW_POLICY
        return {
            "registry_version": REGISTRY_VERSION,
            "governance_version": GOVERNANCE_VERSION,
            "safety_policy_version": PRODUCT_SAFETY_POLICY_VERSION,
            "workflow_policy": {
                "version": workflow.version,
                "invariants": [item.value for item in workflow.invariants],
            },
            "source_refs": [
                REGISTRY_VERSION,
                GOVERNANCE_VERSION,
                PRODUCT_SAFETY_POLICY_VERSION,
                workflow.version,
            ],
        }


def _require_no_arguments(
    arguments: dict[str, Any],
    *,
    tool_name: str,
) -> None:
    if arguments:
        raise ValueError(f"{tool_name} accepts no caller-supplied data")


__all__ = [
    "PRODUCT_RUNTIME_READ_SERVICE_VERSION",
    "CareContextReader",
    "ProductRuntimeReadService",
]
