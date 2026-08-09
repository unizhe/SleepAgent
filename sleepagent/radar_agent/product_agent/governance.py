from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from threading import RLock
from typing import Any, Callable, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AgentEnvelope,
    AgentId,
    CareActionCandidate,
    CareDeliveryDecision,
    CareDeliveryModality,
    CareDeliveryTiming,
    CareStrategy,
    CommunicationDraft,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    FactSnapshot,
    InvocationOutcome,
    InterruptionBurden,
    MemoryChangeCandidate,
    SafetyDecision,
    SafetyVerdict,
    StrictContract,
    ToolEffect,
    ToolReceipt,
    agent_target_hash,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.registry import authorize_tool_invocation
from sleepagent.radar_agent.product_agent.hitl import (
    HumanDecisionError,
    VerifiedApprovalCapability,
)
from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitProfileChangeSet,
    HabitProfileStore,
    InMemoryHabitProfileStore,
)
from sleepagent.radar_agent.product_agent.external_actions import (
    ExternalActionExecutionRequest,
    ExternalActionExecutionResult,
)
from sleepagent.radar_agent.product_agent.longitudinal_memory import (
    GovernedMemoryItemV2,
    LegacyMemoryItemV1,
    MemoryItemRecord,
    MemoryItemStatus,
)
from sleepagent.radar_agent.product_agent.cold_start import (
    CapabilityEligibilityReceipt,
    MetricReadinessDecision,
    ResponseMode,
    claim_strength_allowed,
    degraded_boundary_sentence,
)


GOVERNANCE_VERSION = "sleepagent-product-governance.v20"
PRODUCT_SAFETY_POLICY_VERSION = "product-safety.v3"


class AcceptanceError(ValueError):
    pass


class ConfirmationError(ValueError):
    pass


class StaleStateError(RuntimeError):
    pass


class PublicationError(ValueError):
    pass


class AcceptedWorkProduct(StrictContract):
    work_product_ref: str = Field(..., min_length=1)
    agent_id: AgentId
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    episode_state_revision: int = Field(..., ge=0)
    payload: dict[str, Any]
    accepted_at: datetime


class CareActionDefinition(StrictContract):
    care_action_id: str
    version: int = Field(..., ge=1)
    allowed_parameters: dict[str, tuple[float, float]] = Field(default_factory=dict)
    contraindication_codes: list[str] = Field(default_factory=list)
    delivery_required: bool = False
    allowed_delivery_timings: tuple[CareDeliveryTiming, ...] = ()
    allowed_delivery_modalities: tuple[CareDeliveryModality, ...] = ()


class CareDeliveryPolicy(StrictContract):
    policy_ref: str = "care-delivery-policy.v1"
    device_policy_ref: str = "device-delivery-policy.v1"
    coordination_policy_ref: str = "coordination-policy.v1"
    maximum_nonurgent_voice_volume_percent: int = Field(default=40, ge=0, le=100)
    conservative_timing: Literal["morning"] = "morning"
    conservative_modality: Literal["silent"] = "silent"
    conservative_notify_family: Literal[False] = False
    immediate_quiet_hours_requires_safety_reason: Literal[True] = True

    def conservative_default(self) -> CareDeliveryDecision:
        return CareDeliveryDecision(
            timing=CareDeliveryTiming.MORNING,
            modality=CareDeliveryModality.SILENT,
            interruption_burden=InterruptionBurden.NONE,
            notify_family=False,
            quiet_hours_active=True,
            quiet_hours_override=False,
            conservative_default_applied=True,
            device_policy_ref=self.device_policy_ref,
        )


class CareActionCatalog:
    def __init__(
        self,
        definitions: list[CareActionDefinition] | None = None,
        *,
        delivery_policy: CareDeliveryPolicy | None = None,
    ) -> None:
        definitions = definitions or [
            CareActionDefinition(
                care_action_id="consistent-wake-time",
                version=1,
                allowed_parameters={"tolerance_minutes": (0, 60)},
            ),
            CareActionDefinition(
                care_action_id="morning-light",
                version=1,
                allowed_parameters={"minutes": (5, 45)},
            ),
            CareActionDefinition(
                care_action_id="nighttime-gentle-support",
                version=1,
                delivery_required=True,
                allowed_delivery_timings=(CareDeliveryTiming.IMMEDIATE,),
                allowed_delivery_modalities=(
                    CareDeliveryModality.VOICE,
                    CareDeliveryModality.LIGHT,
                    CareDeliveryModality.SILENT,
                ),
            ),
            CareActionDefinition(
                care_action_id="morning-review-feedback",
                version=1,
                delivery_required=True,
                allowed_delivery_timings=(CareDeliveryTiming.MORNING,),
                allowed_delivery_modalities=(
                    CareDeliveryModality.VOICE,
                    CareDeliveryModality.SILENT,
                ),
            ),
        ]
        self.delivery_policy = delivery_policy or CareDeliveryPolicy()
        self._items = {
            (item.care_action_id, item.version): item for item in definitions
        }

    def list_definitions(self) -> tuple[CareActionDefinition, ...]:
        """Return the reviewed catalog as an immutable, detached snapshot."""

        return tuple(
            self._items[key].model_copy(deep=True)
            for key in sorted(self._items)
        )

    def validate(
        self,
        action: CareActionCandidate,
        *,
        active_constraint_codes: tuple[str, ...] = (),
    ) -> None:
        if not action.activatable:
            return
        definition = self._items.get(
            (action.care_action_id or "", action.care_action_version or 0)
        )
        if definition is None:
            raise AcceptanceError("Care action is not in reviewed catalog")
        delivery = action.delivery
        if definition.delivery_required and delivery is None:
            raise AcceptanceError("Care action requires a delivery decision")
        if delivery is not None:
            if (
                delivery.device_policy_ref
                != self.delivery_policy.device_policy_ref
            ):
                raise AcceptanceError(
                    "Care delivery uses an unreviewed device policy"
                )
            if (
                delivery.notify_family
                and delivery.coordination_policy_ref
                != self.delivery_policy.coordination_policy_ref
            ):
                raise AcceptanceError(
                    "Care delivery uses an unreviewed coordination policy"
                )
            if (
                definition.allowed_delivery_timings
                and delivery.timing not in definition.allowed_delivery_timings
            ):
                raise AcceptanceError("Care delivery timing is not catalog-approved")
            if (
                definition.allowed_delivery_modalities
                and delivery.modality
                not in definition.allowed_delivery_modalities
            ):
                raise AcceptanceError(
                    "Care delivery modality is not catalog-approved"
                )
            if (
                delivery.modality == CareDeliveryModality.VOICE
                and delivery.voice_tone != "urgent"
                and (
                    delivery.voice_volume_percent or 0
                )
                > self.delivery_policy.maximum_nonurgent_voice_volume_percent
            ):
                raise AcceptanceError(
                    "non-urgent voice volume exceeds delivery policy"
                )
        conflicts = set(definition.contraindication_codes).intersection(
            active_constraint_codes
        )
        if conflicts:
            raise AcceptanceError(
                "Care action conflicts with active constraint: "
                + ",".join(sorted(conflicts))
            )
        for name, value in action.parameters.items():
            allowed = definition.allowed_parameters.get(name)
            if allowed is None or not isinstance(value, (int, float)):
                raise AcceptanceError("Care parameter is not catalog-approved")
            if not allowed[0] <= float(value) <= allowed[1]:
                raise AcceptanceError("Care parameter is outside catalog bounds")


def _require_common(envelope: AgentEnvelope, snapshot: FactSnapshot) -> None:
    if envelope.fact_snapshot_id != snapshot.fact_snapshot_id:
        raise AcceptanceError("work product FactSnapshot id mismatch")
    if envelope.fact_snapshot_hash != snapshot.fact_snapshot_hash:
        raise AcceptanceError("work product FactSnapshot hash mismatch")
    if envelope.source_scope != snapshot.source_scope:
        raise AcceptanceError("work product SourceScope mismatch")
    if envelope.status.value != "completed":
        raise AcceptanceError("only completed work products can be accepted")
    if envelope.profile_hash != "0" * 64:
        expected_target_hash = agent_target_hash(
            episode_id=envelope.episode_id,
            target_type=envelope.target_type,
            target_id=envelope.target_id,
            fact_snapshot_id=envelope.fact_snapshot_id,
            fact_snapshot_hash=envelope.fact_snapshot_hash,
            episode_state_revision=envelope.episode_state_revision,
            source_scope=envelope.source_scope,
            input_work_product_refs=envelope.input_work_product_refs,
            agent_id=envelope.agent_id,
            agent_version=envelope.agent_version,
            profile_hash=envelope.profile_hash,
            skill_id=envelope.skill_id,
            skill_version=envelope.skill_version,
            skill_package_hash=envelope.skill_package_hash,
            schema_version=envelope.schema_version,
            policy_version=envelope.policy_version,
            output_payload=envelope.output_payload,
        )
        if envelope.target_hash != expected_target_hash:
            raise AcceptanceError("work product target hash does not bind payload")


def _accepted(envelope: AgentEnvelope) -> AcceptedWorkProduct:
    return AcceptedWorkProduct(
        work_product_ref=f"{envelope.agent_id.value}:{envelope.target_id}:{envelope.target_hash}",
        agent_id=envelope.agent_id,
        target_id=envelope.target_id,
        target_hash=envelope.target_hash,
        fact_snapshot_hash=envelope.fact_snapshot_hash,
        episode_state_revision=envelope.episode_state_revision,
        payload=envelope.output_payload.model_dump(mode="json"),
        accepted_at=datetime.now(timezone.utc),
    )


def accept_evidence(
    envelope: AgentEnvelope,
    *,
    snapshot: FactSnapshot,
    tool_receipts: list[ToolReceipt],
    authorized_user_input_refs: set[str] | None = None,
) -> AcceptedWorkProduct:
    _require_common(envelope, snapshot)
    if envelope.agent_id != AgentId.EVIDENCE_REASONING:
        raise AcceptanceError("Evidence gate only accepts EvidenceReasoningAgent")
    payload = envelope.output_payload
    if not isinstance(payload, EvidencePacket):
        raise AcceptanceError("Evidence gate requires EvidencePacket")
    receipt_refs: set[str] = set()
    for receipt in tool_receipts:
        if receipt.outcome != InvocationOutcome.SUCCEEDED:
            continue
        if receipt.tool_name == "memory.read":
            # A read receipt and EpisodeDigest handles are discovery hints, not
            # current evidence. Only a current governed V2 item can directly
            # support a confirmed-memory claim.
            receipt_refs.update(
                str(item["retrieval_handle"])
                for item in receipt.output.get("items", [])
                if item.get("item_kind") == "governed_memory"
                and item.get("allowed_current_use") == "confirmed_memory"
                and item.get("status") == "active"
            )
            continue
        if receipt.tool_name == "memory.resolve_source":
            receipt_refs.update(
                str(ref) for ref in receipt.output.get("source_refs", [])
            )
            continue
        receipt_refs.update(
            [receipt.tool_invocation_id, *receipt.source_refs]
        )
    canonical_refs = set(snapshot.source_refs) | receipt_refs
    readiness_by_ref = _validated_readiness_receipts(
        snapshot=snapshot,
        tool_receipts=tool_receipts,
    )
    authorized_user_input_refs = authorized_user_input_refs or set()
    for claim in payload.claims:
        _enforce_claim_readiness(
            claim=claim,
            snapshot=snapshot,
            readiness_by_ref=readiness_by_ref,
        )
        if any(
            ref.startswith(("habit-answer:", "habit-fact:"))
            for ref in claim.evidence_refs
        ):
            _reject_prohibited_habit_output(claim.statement)
        if claim.semantic == EvidenceSemantic.USER_REPORTED:
            user_refs = {
                ref
                for ref in claim.evidence_refs
                if ref.startswith("user_report:")
            }
            habit_refs = {
                ref
                for ref in claim.evidence_refs
                if ref.startswith(("habit-answer:", "habit-fact:"))
            }
            if not (
                user_refs.intersection(authorized_user_input_refs)
                or habit_refs.intersection(canonical_refs)
            ):
                raise AcceptanceError("user_reported claim requires user_report source")
            if (
                claim.source_kind == EvidenceSourceKind.CONFIRMED_MEMORY
                and not set(claim.evidence_refs).intersection(canonical_refs)
            ):
                raise AcceptanceError(
                    "confirmed Habit self-report requires current Profile ToolReceipt"
                )
        elif claim.semantic == EvidenceSemantic.OBSERVER_REPORTED:
            if claim.source_kind != EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT:
                raise AcceptanceError(
                    "observer report requires authorized observer source"
                )
            observer_refs = {
                ref
                for ref in claim.evidence_refs
                if ref.startswith("authorized_observer_report:")
            }
            habit_refs = {
                ref
                for ref in claim.evidence_refs
                if ref.startswith(("habit-answer:", "habit-fact:"))
            }
            if len(observer_refs | habit_refs) != len(claim.evidence_refs):
                raise AcceptanceError(
                    "observer report requires a typed authorized source"
                )
            if not (
                observer_refs.intersection(authorized_user_input_refs)
                or habit_refs.intersection(canonical_refs)
            ):
                raise AcceptanceError(
                    "observer report requires authorized capture or user input"
                )
        elif claim.semantic != EvidenceSemantic.UNKNOWN:
            if not set(claim.evidence_refs).intersection(canonical_refs):
                raise AcceptanceError("Evidence claim lacks canonical/ToolReceipt support")
        if claim.source_kind not in {
            EvidenceSourceKind.USER_REPORT,
            EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT,
            EvidenceSourceKind.CONFIRMED_MEMORY,
        }:
            if claim.date_start and snapshot.source_scope.date_start:
                if claim.date_start < snapshot.source_scope.date_start:
                    raise AcceptanceError("Evidence claim expands SourceScope")
            if claim.date_end and snapshot.source_scope.date_end:
                if claim.date_end > snapshot.source_scope.date_end:
                    raise AcceptanceError("Evidence claim expands SourceScope")
    return _accepted(envelope)


def build_cold_start_receipt(
    *,
    snapshot: FactSnapshot,
    decisions: tuple[MetricReadinessDecision, ...],
    capability_receipts: tuple[CapabilityEligibilityReceipt, ...] = (),
    observed_at: datetime,
) -> ToolReceipt:
    """Freeze runtime-generated cold-start inputs in the existing receipt log."""

    decisions = tuple(
        MetricReadinessDecision.model_validate(item.model_dump(mode="json"))
        for item in decisions
    )
    capability_receipts = tuple(
        CapabilityEligibilityReceipt.model_validate(
            item.model_dump(mode="json")
        )
        for item in capability_receipts
    )
    decision_refs = tuple(item.decision_ref for item in decisions)
    decision_hashes = tuple(item.decision_hash for item in decisions)
    capability_refs = tuple(item.receipt_ref for item in capability_receipts)
    capability_hashes = tuple(item.receipt_hash for item in capability_receipts)
    if (
        decision_refs != snapshot.readiness_decision_refs
        or decision_hashes != snapshot.readiness_decision_hashes
        or capability_refs != snapshot.capability_eligibility_refs
        or capability_hashes != snapshot.capability_eligibility_hashes
    ):
        raise AcceptanceError(
            "cold-start receipt does not match frozen FactSnapshot bindings"
        )
    output = {
        "policy_version": (
            decisions[0].policy_version
            if decisions
            else "sleepagent-cold-start.v1"
        ),
        "decisions": [
            item.model_dump(mode="json") for item in decisions
        ],
        "degraded_boundaries": {
            item.decision_ref: degraded_boundary_sentence(item)
            for item in decisions
            if item.response_mode == ResponseMode.DEGRADED
        },
        "capability_eligibility_receipts": [
            item.model_dump(mode="json") for item in capability_receipts
        ],
    }
    source_refs = tuple(
        dict.fromkeys(
            ref
            for item in decisions
            for ref in (
                *item.scope_source_refs,
                *item.baseline_source_refs,
            )
        )
    )
    return ToolReceipt(
        tool_invocation_id=(
            f"cold-start:{stable_hash((snapshot.fact_snapshot_hash, output))[:24]}"
        ),
        tool_name="cold_start.evaluate",
        tool_version="sleepagent-cold-start.v1",
        caller="runtime",
        fact_snapshot_id=snapshot.fact_snapshot_id,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        input_hash=stable_hash(
            {
                "decision_refs": decision_refs,
                "capability_refs": capability_refs,
            }
        ),
        effect=ToolEffect.READ_ONLY,
        outcome=InvocationOutcome.SUCCEEDED,
        observed_at=observed_at,
        output=output,
        source_refs=list(source_refs[:50]),
    )


def _validated_readiness_receipts(
    *,
    snapshot: FactSnapshot,
    tool_receipts: list[ToolReceipt],
) -> dict[str, MetricReadinessDecision]:
    if not snapshot.readiness_decision_refs:
        return {}
    expected = dict(
        zip(
            snapshot.readiness_decision_refs,
            snapshot.readiness_decision_hashes,
        )
    )
    found: dict[str, MetricReadinessDecision] = {}
    for receipt in tool_receipts:
        if (
            receipt.tool_name != "cold_start.evaluate"
            or receipt.outcome != InvocationOutcome.SUCCEEDED
        ):
            continue
        if (
            receipt.fact_snapshot_id != snapshot.fact_snapshot_id
            or receipt.fact_snapshot_hash != snapshot.fact_snapshot_hash
        ):
            raise AcceptanceError("cold-start receipt is stale")
        for raw in receipt.output.get("decisions", ()):
            try:
                decision = MetricReadinessDecision.model_validate(raw)
            except ValueError as exc:
                raise AcceptanceError(
                    "cold-start receipt contains an invalid decision"
                ) from exc
            ref = decision.decision_ref
            if ref not in expected or expected[ref] != decision.decision_hash:
                raise AcceptanceError(
                    "cold-start decision is not bound to FactSnapshot"
                )
            if ref in found:
                raise AcceptanceError("duplicate cold-start decision receipt")
            found[ref] = decision
    if set(found) != set(expected):
        raise AcceptanceError(
            "FactSnapshot readiness decisions lack typed ToolReceipt evidence"
        )
    return found


def _enforce_claim_readiness(
    *,
    claim: Any,
    snapshot: FactSnapshot,
    readiness_by_ref: dict[str, MetricReadinessDecision],
) -> None:
    personal_night_claim = (
        claim.source_kind
        in {
            EvidenceSourceKind.CANONICAL_OBSERVATION,
            EvidenceSourceKind.TREND_TOOL,
            EvidenceSourceKind.DATA_QUALITY,
        }
        and claim.semantic
        in {
            EvidenceSemantic.OBSERVED_FACT,
            EvidenceSemantic.INFERENCE,
        }
    )
    if not readiness_by_ref:
        if claim.readiness_decision_ref:
            raise AcceptanceError(
                "claim cites readiness decision absent from FactSnapshot"
            )
        return
    if not personal_night_claim and not claim.readiness_decision_ref:
        return
    if not all(
        (
            claim.claim_strength,
            claim.metric_id,
            claim.measurement_cohort_ref,
            claim.readiness_decision_ref,
        )
    ):
        raise AcceptanceError(
            "personal claim lacks typed cold-start readiness binding"
        )
    decision = readiness_by_ref.get(claim.readiness_decision_ref)
    if decision is None:
        raise AcceptanceError("claim readiness decision is not frozen")
    if claim.metric_id != decision.metric_id:
        raise AcceptanceError("claim metric does not match readiness decision")
    if claim.measurement_cohort_ref != decision.measurement_cohort_ref:
        raise AcceptanceError(
            "claim measurement cohort does not match readiness decision"
        )
    if not claim_strength_allowed(claim.claim_strength, decision):
        raise AcceptanceError("claim strength exceeds cold-start ceiling")
    if claim.date_start and decision.scope_date_start:
        if claim.date_start < decision.scope_date_start:
            raise AcceptanceError("claim expands readiness date scope")
    if claim.date_end and decision.scope_date_end:
        if claim.date_end > decision.scope_date_end:
            raise AcceptanceError("claim expands readiness date scope")


def accept_care(
    envelope: AgentEnvelope,
    *,
    snapshot: FactSnapshot,
    accepted_evidence: AcceptedWorkProduct,
    catalog: CareActionCatalog,
    active_primary_action_id: str | None = None,
) -> AcceptedWorkProduct:
    _require_common(envelope, snapshot)
    if envelope.agent_id != AgentId.CARE_STRATEGY:
        raise AcceptanceError("Care gate only accepts CareStrategyAgent")
    payload = envelope.output_payload
    if not isinstance(payload, CareStrategy):
        raise AcceptanceError("Care gate requires CareStrategy")
    if accepted_evidence.agent_id != AgentId.EVIDENCE_REASONING:
        raise AcceptanceError("Care requires accepted Evidence")
    if accepted_evidence.fact_snapshot_hash != snapshot.fact_snapshot_hash:
        raise AcceptanceError("Care references stale Evidence")
    if accepted_evidence.work_product_ref not in envelope.input_work_product_refs:
        raise AcceptanceError("Care does not reference accepted Evidence")
    if payload.evidence_packet_refs != [accepted_evidence.work_product_ref]:
        raise AcceptanceError("Care payload must bind the exact accepted Evidence")
    if payload.primary_action:
        accepted_claim_ids = {
            item["claim_id"]
            for item in accepted_evidence.payload.get("claims", [])
        }
        if not set(payload.primary_action.rationale_evidence_refs).issubset(
            accepted_claim_ids
        ):
            raise AcceptanceError("Care rationale references unaccepted Evidence")
        delivery = payload.primary_action.delivery
        if delivery and not set(delivery.preference_evidence_refs).issubset(
            accepted_claim_ids
        ):
            raise AcceptanceError(
                "Care delivery preference references unaccepted Evidence"
            )
        if delivery and delivery.notify_family and not any(
            candidate.recipient_role == "family"
            for candidate in payload.coordination_candidates
        ):
            raise AcceptanceError(
                "family notification requires a family CoordinationCandidate"
            )
        catalog.validate(
            payload.primary_action,
            active_constraint_codes=snapshot.active_constraint_codes,
        )
        if active_primary_action_id and payload.disposition == "propose":
            raise AcceptanceError("a second primary Care action is forbidden")
    if payload.disposition in {"adjust", "pause", "complete", "end"} and not (
        active_primary_action_id
    ):
        raise AcceptanceError("Care transition requires an active primary action")
    return _accepted(envelope)


def accept_safety(
    envelope: AgentEnvelope,
    *,
    snapshot: FactSnapshot,
    target: AcceptedWorkProduct,
    now: datetime | None = None,
) -> AcceptedWorkProduct:
    _require_common(envelope, snapshot)
    if envelope.agent_id != AgentId.SAFETY_REVIEW:
        raise AcceptanceError("Safety gate only accepts SafetyReviewAgent")
    payload = envelope.output_payload
    if not isinstance(payload, SafetyDecision):
        raise AcceptanceError("Safety gate requires SafetyDecision")
    if payload.review_target_id != target.target_id:
        raise AcceptanceError("Safety target id mismatch")
    if payload.review_target_hash != target.target_hash:
        raise AcceptanceError("Safety target hash mismatch")
    if payload.fact_snapshot_hash != snapshot.fact_snapshot_hash:
        raise AcceptanceError("Safety FactSnapshot mismatch")
    if payload.reviewed_episode_state_revision != target.episode_state_revision:
        raise AcceptanceError("Safety target revision mismatch")
    if payload.policy_version != PRODUCT_SAFETY_POLICY_VERSION:
        raise AcceptanceError("Safety policy version mismatch")
    if payload.expires_at <= (now or datetime.now(timezone.utc)):
        raise AcceptanceError("Safety decision expired")
    return _accepted(envelope)


def accept_communication(
    envelope: AgentEnvelope,
    *,
    snapshot: FactSnapshot,
    accepted_evidence: AcceptedWorkProduct | None = None,
    accepted_care: AcceptedWorkProduct | None = None,
    reviewed_knowledge_refs: set[str] | None = None,
    reviewed_knowledge_payloads: dict[str, Any] | None = None,
    require_personal_grounding: bool = False,
    authenticated_user_text: str = "",
    expected_audience_role: str | None = None,
) -> AcceptedWorkProduct:
    _require_common(envelope, snapshot)
    if envelope.agent_id != AgentId.SLEEP_CARE:
        raise AcceptanceError("Communication gate only accepts SleepCareAgent")
    payload = envelope.output_payload
    if not isinstance(payload, CommunicationDraft):
        raise AcceptanceError("Communication gate requires CommunicationDraft")
    if (
        expected_audience_role is not None
        and payload.audience_role != expected_audience_role
    ):
        raise AcceptanceError("Communication audience role mismatch")
    evidence_claims = {
        item["claim_id"]
        for item in (accepted_evidence.payload.get("claims", []) if accepted_evidence else [])
    }
    if not set(payload.claim_refs).issubset(evidence_claims):
        raise AcceptanceError("Communication introduces an unaccepted claim")
    allowed_care: set[str] = set()
    if accepted_care:
        action = accepted_care.payload.get("primary_action")
        if action:
            allowed_care.add(action["candidate_id"])
    if not set(payload.care_candidate_refs).issubset(allowed_care):
        raise AcceptanceError("Communication changes or invents a Care candidate")
    bindings_by_ref: dict[str, list[Any]] = {}
    for binding in payload.semantic_bindings:
        bindings_by_ref.setdefault(binding.source_ref, []).append(binding)
        if binding.rendered_text not in payload.text:
            raise AcceptanceError(
                "Communication semantic binding is not present in rendered text"
            )
    if not set(payload.claim_refs).issubset(bindings_by_ref):
        raise AcceptanceError("Communication claim lacks an exact semantic binding")
    if not set(payload.care_candidate_refs).issubset(bindings_by_ref):
        raise AcceptanceError("Communication Care action lacks semantic binding")
    if require_personal_grounding and accepted_evidence:
        has_personal_claims = bool(accepted_evidence.payload.get("claims"))
        if has_personal_claims and not payload.claim_refs:
            raise AcceptanceError(
                "personalized Communication must cite accepted Evidence"
            )
    evidence_by_id = {
        item["claim_id"]: item
        for item in (accepted_evidence.payload.get("claims", []) if accepted_evidence else [])
    }
    care_by_id = {}
    if accepted_care and accepted_care.payload.get("primary_action"):
        action = accepted_care.payload["primary_action"]
        care_by_id[action["candidate_id"]] = action
    reviewed_knowledge_refs = reviewed_knowledge_refs or set()
    reviewed_knowledge_payloads = reviewed_knowledge_payloads or {}
    bound_numbers: set[str] = set()
    for binding in payload.semantic_bindings:
        if binding.source_kind == "evidence_claim":
            source = evidence_by_id.get(binding.source_ref)
            if source is None:
                raise AcceptanceError("Communication binding cites unknown Evidence")
        elif binding.source_kind == "care_candidate":
            source = care_by_id.get(binding.source_ref)
            if source is None:
                raise AcceptanceError("Communication binding cites unknown Care action")
        else:
            if binding.source_ref not in reviewed_knowledge_refs:
                raise AcceptanceError(
                    "general knowledge binding lacks reviewed ToolReceipt"
                )
            source = reviewed_knowledge_payloads.get(binding.source_ref)
            if source is None:
                raise AcceptanceError(
                    "general knowledge binding lacks reviewed source payload"
                )
        source_numbers = _numeric_tokens(str(source))
        rendered_numbers = _numeric_tokens(binding.rendered_text)
        if set(binding.preserved_numbers) != rendered_numbers:
            raise AcceptanceError(
                "Communication binding must enumerate every rendered number"
            )
        if not rendered_numbers.issubset(source_numbers):
            raise AcceptanceError(
                "Communication changes or invents an upstream number"
            )
        bound_numbers.update(rendered_numbers)
    if not _numeric_tokens(payload.text).issubset(bound_numbers):
        raise AcceptanceError("Communication contains an unbound number")
    explicit_memory_markers = (
        "记住",
        "修改",
        "更正",
        "忘记",
        "remember",
        "update",
        "correct",
        "forget",
    )
    explicit_memory_intent = any(
        marker in authenticated_user_text.lower()
        for marker in explicit_memory_markers
    )
    expected_user_ref = f"user_report:{stable_hash(authenticated_user_text)[:16]}"
    for candidate in payload.memory_change_candidates:
        if candidate.subject_id != snapshot.binding.subject_id:
            raise AcceptanceError("Memory candidate subject mismatch")
        if snapshot.binding.role != "elder":
            raise AcceptanceError("only the authenticated elder may change Memory")
        if candidate.explicit_user_authorization:
            if not explicit_memory_intent:
                raise AcceptanceError(
                    "Memory authorization must come from the outer user message"
                )
            if candidate.source_ref != expected_user_ref:
                raise AcceptanceError(
                    "Memory authorization source does not bind outer user message"
                )
        elif not candidate.confirmation_required:
            raise AcceptanceError(
                "inferred Memory candidates require separate confirmation"
            )
    if accepted_evidence and any(
        ref.startswith(("habit-answer:", "habit-fact:"))
        for claim in accepted_evidence.payload.get("claims", [])
        for ref in claim.get("evidence_refs", [])
    ):
        _reject_prohibited_habit_output(payload.text)
    return _accepted(envelope)


def _numeric_tokens(text: str) -> set[str]:
    return set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?%?", text))


def _reject_prohibited_habit_output(text: str) -> None:
    lowered = text.lower()
    forbidden = (
        "睡眠习惯综合分",
        "睡眠习惯评分",
        "固定睡眠类型",
        "坏习惯标签",
        "因此确诊",
        "习惯导致了",
        "habit score",
        "fixed sleep type",
        "caused by this habit",
    )
    if any(marker in lowered for marker in forbidden):
        raise AcceptanceError(
            "Habit context cannot produce score, fixed label, diagnosis, or causality"
        )


def safety_trigger_reasons(
    *,
    target: AcceptedWorkProduct,
    episode_type: str,
    external_action: bool = False,
    doctor_material: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if target.agent_id == AgentId.EVIDENCE_REASONING:
        claims = target.payload.get("claims", [])
        if target.payload.get("conflicts") or any(
            item.get("semantic") == EvidenceSemantic.INFERENCE.value
            and float(item.get("confidence", 0)) < 0.75
            for item in claims
        ):
            reasons.append("conflicting_or_low_confidence_personal_claim")
    if target.agent_id == AgentId.CARE_STRATEGY:
        action = target.payload.get("primary_action")
        if action and not action.get("activatable"):
            reasons.append("outside_reviewed_care_catalog")
    text = str(target.payload).lower()
    if any(word in text for word in ("diagnosis", "diagnose", "药物", "停药", "确诊")):
        reasons.append("restricted_medical_language")
    if doctor_material:
        reasons.append("doctor_material")
    if external_action:
        reasons.append("external_action")
    return sorted(set(reasons))


def require_safety_approval(
    target: AcceptedWorkProduct,
    safety: AcceptedWorkProduct | None,
) -> None:
    if safety is None:
        raise AcceptanceError("required Safety review is missing")
    payload = SafetyDecision.model_validate(safety.payload)
    if payload.review_target_hash != target.target_hash:
        raise AcceptanceError("Safety approval is stale")
    if payload.verdict != SafetyVerdict.APPROVE:
        raise AcceptanceError(f"Safety did not approve target: {payload.verdict.value}")


class CareContextState(StrictContract):
    subject_id: str
    version: int = Field(default=0, ge=0)
    active_primary_action: dict[str, Any] | None = None
    transition_history: list["CareTransitionEvent"] = Field(
        default_factory=list,
        max_length=200,
    )


class CareTransitionEvent(StrictContract):
    strategy_id: str = Field(..., min_length=1)
    disposition: Literal["propose", "adjust", "pause", "complete", "end"]
    from_candidate_id: str | None = None
    to_candidate_id: str | None = None
    evidence_packet_refs: list[str] = Field(default_factory=list, max_length=10)
    committed_at: datetime


MemoryItem = LegacyMemoryItemV1


class MemoryContextState(StrictContract):
    subject_id: str
    version: int = Field(default=0, ge=0)
    items: list[MemoryItemRecord] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def classify_legacy_rows(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        items = []
        for item in normalized.get("items", []):
            if isinstance(item, dict) and "schema_version" not in item:
                item = {"schema_version": "LegacyMemoryItem.v1", **item}
            items.append(item)
        normalized["items"] = items
        return normalized


class CommitJournalEntry(StrictContract):
    idempotency_key: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    input_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    authority_refs: tuple[str, ...] = ()
    state: Literal["pending", "final"]
    receipt: ToolReceipt | None = None
    created_at: datetime
    updated_at: datetime


class CommitJournal(Protocol):
    def get(self, idempotency_key: str) -> CommitJournalEntry | None: ...

    def reserve(self, entry: CommitJournalEntry) -> bool: ...

    def finalize(self, entry: CommitJournalEntry) -> None: ...


class InMemoryCommitJournal:
    def __init__(self) -> None:
        self._items: dict[str, CommitJournalEntry] = {}
        self.lock = RLock()

    def get(self, idempotency_key: str) -> CommitJournalEntry | None:
        with self.lock:
            entry = self._items.get(idempotency_key)
            return None if entry is None else entry.model_copy(deep=True)

    def reserve(self, entry: CommitJournalEntry) -> bool:
        if entry.state != "pending" or entry.receipt is not None:
            raise ValueError("commit reservation must be pending")
        with self.lock:
            if entry.idempotency_key in self._items:
                return False
            self._items[entry.idempotency_key] = entry.model_copy(deep=True)
            return True

    def finalize(self, entry: CommitJournalEntry) -> None:
        if entry.state != "final" or entry.receipt is None:
            raise ValueError("final commit journal entry requires a receipt")
        with self.lock:
            current = self._items.get(entry.idempotency_key)
            if (
                current is None
                or current.state != "pending"
                or current.tool_name != entry.tool_name
                or current.input_hash != entry.input_hash
                or current.fact_snapshot_hash != entry.fact_snapshot_hash
            ):
                raise StaleStateError("commit journal reservation changed")
            self._items[entry.idempotency_key] = entry.model_copy(deep=True)


class InMemoryCareContextStore:
    def __init__(self) -> None:
        self._items: dict[str, CareContextState] = {}
        self.lock = RLock()

    def get(self, subject_id: str) -> CareContextState:
        return self._items.get(
            subject_id, CareContextState(subject_id=subject_id)
        ).model_copy(deep=True)

    def compare_and_set(
        self, subject_id: str, expected_version: int, state: CareContextState
    ) -> None:
        current = self.get(subject_id)
        if current.version != expected_version:
            raise StaleStateError("stale Care Context")
        if state.version != expected_version + 1:
            raise StaleStateError("Care Context version must advance exactly once")
        self._items[subject_id] = state.model_copy(deep=True)


class InMemoryMemoryContextStore:
    def __init__(
        self,
        *,
        control_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._items: dict[str, MemoryContextState] = {}
        self.lock = RLock()
        self._control_clock = control_clock or (
            lambda: datetime.now(timezone.utc)
        )

    def get(self, subject_id: str) -> MemoryContextState:
        return self._items.get(
            subject_id, MemoryContextState(subject_id=subject_id)
        ).model_copy(deep=True)

    def compare_and_set(
        self,
        subject_id: str,
        expected_version: int,
        state: MemoryContextState,
    ) -> None:
        current = self.get(subject_id)
        if current.version != expected_version:
            raise StaleStateError("stale Memory Context")
        if state.version != expected_version + 1:
            raise StaleStateError("Memory Context version must advance exactly once")
        self._items[subject_id] = state.model_copy(deep=True)

    def apply(
        self,
        candidate: MemoryChangeCandidate,
        *,
        expected_version: int,
        confirmed: bool,
        fact_snapshot: FactSnapshot,
        confirmation_ref: str,
    ) -> MemoryContextState:
        if candidate.candidate_id.startswith(("habit:", "habit-", "habit_")) or (
            candidate.source_ref.startswith(
                ("habit-answer:", "habit-fact:", "habit-candidate:")
            )
        ):
            raise ConfirmationError(
                "Habit Profile data must use state.commit_habit_profile"
            )
        with self.lock:
            state = self.get(candidate.subject_id)
            if state.version != expected_version:
                raise StaleStateError("stale Memory Context")
            if candidate.confirmation_required and not confirmed:
                raise ConfirmationError("Memory candidate requires confirmation")
            items = [item.model_copy(deep=True) for item in state.items]
            matching_indices = [
                index
                for index, item in enumerate(items)
                if item.memory_id == candidate.candidate_id
            ]
            existing_index = (
                max(matching_indices, key=lambda index: items[index].version)
                if matching_indices
                else None
            )
            if candidate.operation == "create" and existing_index is not None:
                raise StaleStateError("Memory item already exists")
            if candidate.operation != "create" and existing_index is None:
                raise StaleStateError("Memory item does not exist")
            prior = items[existing_index] if existing_index is not None else None
            status = {
                "create": MemoryItemStatus.ACTIVE,
                "replace": MemoryItemStatus.ACTIVE,
                "expire": MemoryItemStatus.EXPIRED,
                "forget": MemoryItemStatus.FORGOTTEN,
            }[candidate.operation]
            recorded_at = self._control_clock()
            if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
                raise ValueError("Memory control clock must return aware time")
            recorded_at = recorded_at.astimezone(timezone.utc)
            governed = GovernedMemoryItemV2(
                memory_id=candidate.candidate_id,
                subject_id=candidate.subject_id,
                memory_type=candidate.memory_type,
                concept_id=candidate.concept_id,
                value_schema_id=candidate.value_schema_id,
                value_schema_version=candidate.value_schema_version,
                typed_value=candidate.typed_value,
                provenance_type=candidate.provenance_type,
                source_ref=candidate.source_ref,
                source_scope_kind=fact_snapshot.source_scope.kind,
                version=(prior.version + 1 if prior is not None else 1),
                recorded_at=recorded_at,
                valid_from=recorded_at,
                valid_until=candidate.valid_until,
                sensitivity_class=candidate.sensitivity_class,
                allowed_roles=candidate.allowed_roles,
                allowed_purposes=candidate.allowed_purposes,
                status=status,
                supersedes_ref=(
                    f"{prior.memory_id}:v{prior.version}"
                    if prior is not None
                    else None
                ),
                confirmation_ref=confirmation_ref,
                retention_policy_version=candidate.retention_policy_version,
            )
            if existing_index is None:
                items.append(governed)
            else:
                # Governed revisions are append-only. Retrieval materializes
                # only the latest version; older payloads remain audit state.
                items.append(governed)
            updated = MemoryContextState(
                subject_id=state.subject_id,
                version=state.version + 1,
                items=items,
            )
            self.compare_and_set(state.subject_id, expected_version, updated)
            return updated


class DeterministicCommitController:
    def __init__(
        self,
        *,
        care_store: InMemoryCareContextStore | None = None,
        memory_store: InMemoryMemoryContextStore | None = None,
        habit_profile_store: HabitProfileStore | None = None,
        commit_journal: CommitJournal | None = None,
    ) -> None:
        self.care_store = care_store or InMemoryCareContextStore()
        self.memory_store = memory_store or InMemoryMemoryContextStore()
        self.habit_profile_store = habit_profile_store or InMemoryHabitProfileStore()
        self.commit_journal = commit_journal or InMemoryCommitJournal()
        self._lock = RLock()

    def activate_care(
        self,
        *,
        action: CareActionCandidate,
        subject_id: str,
        expected_version: int,
        fact_snapshot: FactSnapshot,
        idempotency_key: str,
        approval_capability: VerifiedApprovalCapability,
    ) -> ToolReceipt:
        if not action.activatable:
            raise ConfirmationError("non-catalog Care candidate cannot be activated")
        self._validate_approval_capability(
            approval_capability,
            target_id=action.candidate_id,
            target_hash=action.candidate_hash,
            subject_id=subject_id,
            action_scope="activate_care",
            fact_snapshot=fact_snapshot,
            idempotency_key=idempotency_key,
            elder_required=True,
        )
        return self._commit(
            tool_name="state.commit_care",
            idempotency_key=idempotency_key,
            snapshot=fact_snapshot,
            payload=action.model_dump(mode="json"),
            mutate=lambda: self._activate(
                subject_id, expected_version, action.model_dump(mode="json")
            ),
            approval_capability=approval_capability,
        )

    def transition_care(
        self,
        *,
        strategy: CareStrategy,
        strategy_target_hash: str,
        subject_id: str,
        expected_version: int,
        fact_snapshot: FactSnapshot,
        idempotency_key: str,
        approval_capability: VerifiedApprovalCapability,
    ) -> ToolReceipt:
        if strategy.disposition not in {
            "adjust",
            "pause",
            "complete",
            "end",
        }:
            raise ConfirmationError("unsupported Care transition")
        self._validate_approval_capability(
            approval_capability,
            target_id=strategy.strategy_id,
            target_hash=strategy_target_hash,
            subject_id=subject_id,
            action_scope="transition_care",
            fact_snapshot=fact_snapshot,
            idempotency_key=idempotency_key,
            elder_required=True,
        )
        return self._commit(
            tool_name="state.commit_care",
            idempotency_key=idempotency_key,
            snapshot=fact_snapshot,
            payload={
                "strategy": strategy.model_dump(mode="json"),
                "strategy_target_hash": strategy_target_hash,
            },
            mutate=lambda: self._transition_care_state(
                subject_id=subject_id,
                expected_version=expected_version,
                strategy=strategy,
            ),
            approval_capability=approval_capability,
        )

    def _transition_care_state(
        self,
        *,
        subject_id: str,
        expected_version: int,
        strategy: CareStrategy,
    ) -> dict[str, Any]:
        with self.care_store.lock:
            current = self.care_store.get(subject_id)
            if current.version != expected_version:
                raise StaleStateError("stale Care Context")
            if current.active_primary_action is None:
                raise StaleStateError("Care transition requires an active action")
            next_action = (
                strategy.primary_action.model_dump(mode="json")
                if strategy.disposition == "adjust" and strategy.primary_action
                else None
            )
            previous_id = str(
                current.active_primary_action.get("candidate_id")
            )
            next_id = (
                str(next_action.get("candidate_id"))
                if next_action is not None
                else None
            )
            transition = CareTransitionEvent(
                strategy_id=strategy.strategy_id,
                disposition=strategy.disposition,
                from_candidate_id=previous_id,
                to_candidate_id=next_id,
                evidence_packet_refs=list(strategy.evidence_packet_refs),
                committed_at=datetime.now(timezone.utc),
            )
            updated = CareContextState(
                subject_id=subject_id,
                version=expected_version + 1,
                active_primary_action=next_action,
                transition_history=[
                    *current.transition_history,
                    transition,
                ][-200:],
            )
            self.care_store.compare_and_set(subject_id, expected_version, updated)
            return {
                "care_context_version": updated.version,
                "disposition": strategy.disposition,
            }

    def execute_external(
        self,
        *,
        tool_name: str,
        target: dict[str, Any],
        snapshot: FactSnapshot,
        idempotency_key: str,
        executor: Callable[
            [ExternalActionExecutionRequest],
            ExternalActionExecutionResult,
        ],
        approval_capability: VerifiedApprovalCapability,
        actor_id: str,
        subject_id: str,
        action_scope: str,
        target_id: str,
        target_version: int,
        target_hash: str,
    ) -> ToolReceipt:
        if tool_name not in {"external.notify", "external.share", "external.export"}:
            raise ValueError("unsupported external action")
        if actor_id != snapshot.binding.actor_id:
            raise ConfirmationError("external action actor mismatch")
        if subject_id != snapshot.binding.subject_id:
            raise ConfirmationError("external action subject mismatch")
        self._validate_approval_capability(
            approval_capability,
            target_id=target_id,
            target_hash=target_hash,
            subject_id=subject_id,
            action_scope=action_scope,
            fact_snapshot=snapshot,
            idempotency_key=idempotency_key,
            elder_required=True,
        )
        commit_payload = {
            "tool_name": tool_name,
            "target_id": target_id,
            "target_version": target_version,
            "target_hash": target_hash,
            "actor_id": actor_id,
            "subject_id": subject_id,
            "action_scope": action_scope,
            "payload": target,
        }
        return self._commit(
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            payload=commit_payload,
            mutate=lambda: executor(
                ExternalActionExecutionRequest(
                    tool_name=tool_name,
                    target_id=target_id,
                    target_version=target_version,
                    target_hash=target_hash,
                    actor_id=actor_id,
                    subject_id=subject_id,
                    action_scope=action_scope,
                    fact_snapshot_hash=snapshot.fact_snapshot_hash,
                    idempotency_key=idempotency_key,
                    payload=target,
                )
            ).model_dump(mode="json"),
            approval_capability=approval_capability,
        )

    def commit_memory(
        self,
        *,
        candidate: MemoryChangeCandidate,
        expected_version: int,
        fact_snapshot: FactSnapshot,
        idempotency_key: str,
        approval_capability: VerifiedApprovalCapability,
    ) -> ToolReceipt:
        if candidate.subject_id != fact_snapshot.binding.subject_id:
            raise ConfirmationError("Memory candidate subject mismatch")
        self._validate_approval_capability(
            approval_capability,
            target_id=candidate.candidate_id,
            target_hash=str(candidate.candidate_hash),
            subject_id=fact_snapshot.binding.subject_id,
            action_scope="commit_memory",
            fact_snapshot=fact_snapshot,
            idempotency_key=idempotency_key,
            elder_required=True,
        )
        return self._commit(
            tool_name="state.commit_memory",
            idempotency_key=idempotency_key,
            snapshot=fact_snapshot,
            payload=candidate.model_dump(mode="json"),
            mutate=lambda: self.memory_store.apply(
                candidate,
                expected_version=expected_version,
                confirmed=True,
                fact_snapshot=fact_snapshot,
                confirmation_ref=approval_capability.grant.grant_id,
            ).model_dump(mode="json"),
            approval_capability=approval_capability,
        )

    def commit_habit_profile(
        self,
        *,
        change_set: HabitProfileChangeSet,
        approval_capability: VerifiedApprovalCapability,
        fact_snapshot: FactSnapshot,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ToolReceipt:
        """Commit the elder-confirmed manifest atomically through one writer."""

        committed_at = now or datetime.now(timezone.utc)
        payload = {"change_set": change_set.model_dump(mode="json")}
        if change_set.fact_snapshot_hash != fact_snapshot.fact_snapshot_hash:
            raise ConfirmationError("Habit change set FactSnapshot mismatch")
        if change_set.subject_id != fact_snapshot.binding.subject_id:
            raise ConfirmationError("Habit change set subject mismatch")
        if fact_snapshot.binding.role != "elder":
            raise ConfirmationError("only the authenticated elder may confirm Profile")
        if change_set.expected_memory_version != fact_snapshot.memory_context_version:
            raise StaleStateError("Habit change set memory version is stale")
        self._validate_approval_capability(
            approval_capability,
            target_id=change_set.change_set_id,
            target_hash=change_set.manifest_hash,
            subject_id=change_set.subject_id,
            action_scope=change_set.action_scope,
            fact_snapshot=fact_snapshot,
            idempotency_key=idempotency_key,
            elder_required=True,
            approver_must_match_snapshot=True,
            now=committed_at,
        )
        return self._commit(
            tool_name="state.commit_habit_profile",
            idempotency_key=idempotency_key,
            snapshot=fact_snapshot,
            payload=payload,
            mutate=lambda: self.habit_profile_store.commit(
                change_set,
                approval_capability,
                idempotency_key=idempotency_key,
                now=committed_at,
            ).model_dump(mode="json"),
            approval_capability=approval_capability,
        )

    @staticmethod
    def _validate_approval_capability(
        approval_capability: VerifiedApprovalCapability,
        *,
        target_id: str,
        target_hash: str,
        subject_id: str,
        action_scope: str,
        fact_snapshot: FactSnapshot,
        idempotency_key: str,
        elder_required: bool,
        approver_must_match_snapshot: bool = False,
        now: datetime | None = None,
    ) -> None:
        if type(approval_capability) is not VerifiedApprovalCapability:
            raise ConfirmationError(
                "commit requires an authority-verified approval capability"
            )
        grant = approval_capability.grant
        if elder_required and grant.approver_role != "elder":
            raise ConfirmationError("only the elder owner may approve this commit")
        if approver_must_match_snapshot and (
            grant.approver_actor_id != fact_snapshot.binding.actor_id
            or grant.approver_role != fact_snapshot.binding.role
        ):
            raise ConfirmationError(
                "approval actor does not match the authenticated FactSnapshot"
            )
        try:
            approval_capability.validate_exact_binding(
                decision_id=grant.decision_id,
                proposal_id=grant.proposal_id,
                actor_id=grant.approver_actor_id,
                actor_role=grant.approver_role,
                subject_id=subject_id,
                target_id=target_id,
                target_hash=target_hash,
                action_scope=action_scope,
                fact_snapshot_id=fact_snapshot.fact_snapshot_id,
                fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
                # HDS validated the then-current policy before the atomic
                # APPROVED -> EXECUTING transition.  Commits and crash
                # recovery must remain bound to that persisted grant rather
                # than reopening authority against a later deployment policy.
                policy_version=grant.policy_version,
                idempotency_key=idempotency_key,
                now=now,
            )
        except HumanDecisionError as exc:
            raise ConfirmationError(
                "approval capability is stale or bound to another commit"
            ) from exc

    def _activate(
        self, subject_id: str, expected_version: int, action: dict[str, Any]
    ) -> dict[str, Any]:
        with self.care_store.lock:
            current = self.care_store.get(subject_id)
            if current.active_primary_action is not None:
                raise StaleStateError("one primary Care action is already active")
            updated = CareContextState(
                subject_id=subject_id,
                version=expected_version + 1,
                active_primary_action=action,
                transition_history=[
                    *current.transition_history,
                    CareTransitionEvent(
                        strategy_id=f"activation:{action['candidate_id']}",
                        disposition="propose",
                        to_candidate_id=str(action["candidate_id"]),
                        evidence_packet_refs=[],
                        committed_at=datetime.now(timezone.utc),
                    ),
                ][-200:],
            )
            self.care_store.compare_and_set(subject_id, expected_version, updated)
            return {"care_context_version": updated.version}

    def _commit(
        self,
        *,
        tool_name: str,
        idempotency_key: str,
        snapshot: FactSnapshot,
        payload: dict[str, Any],
        mutate: Callable[[], dict[str, Any]],
        approval_capability: VerifiedApprovalCapability,
    ) -> ToolReceipt:
        authorize_tool_invocation("commit_controller", tool_name)
        grant = approval_capability.grant
        authority_refs = (
            f"human-decision:{grant.decision_id}",
            f"approval-grant:{grant.grant_id}:{grant.grant_hash}",
        )
        input_hash = stable_hash(
            {
                "operation": payload,
                "authority": {
                    "decision_id": grant.decision_id,
                    "proposal_id": grant.proposal_id,
                    "grant_id": grant.grant_id,
                    "grant_hash": grant.grant_hash,
                    "approving_records_hash": grant.approving_records_hash,
                },
            }
        )
        with self._lock:
            prior = self.commit_journal.get(idempotency_key)
            if prior is not None:
                return self._receipt_from_journal(
                    prior,
                    tool_name=tool_name,
                    input_hash=input_hash,
                    snapshot=snapshot,
                )
            reserved_at = datetime.now(timezone.utc)
            reservation = CommitJournalEntry(
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                input_hash=input_hash,
                fact_snapshot_hash=snapshot.fact_snapshot_hash,
                authority_refs=authority_refs,
                state="pending",
                created_at=reserved_at,
                updated_at=reserved_at,
            )
            if not self.commit_journal.reserve(reservation):
                prior = self.commit_journal.get(idempotency_key)
                if prior is None:
                    raise StaleStateError("commit journal reservation disappeared")
                return self._receipt_from_journal(
                    prior,
                    tool_name=tool_name,
                    input_hash=input_hash,
                    snapshot=snapshot,
                )
            try:
                output = mutate()
                outcome = InvocationOutcome.SUCCEEDED
                error_code = None
            except Exception as exc:
                output = {}
                outcome = InvocationOutcome.UNKNOWN
                error_code = type(exc).__name__
            receipt = ToolReceipt(
                tool_invocation_id=f"commit:{stable_hash(idempotency_key)[:16]}",
                tool_name=tool_name,
                tool_version="v1",
                caller="commit_controller",
                fact_snapshot_id=snapshot.fact_snapshot_id,
                fact_snapshot_hash=snapshot.fact_snapshot_hash,
                input_hash=input_hash,
                effect=(
                    ToolEffect.STATE_WRITE
                    if tool_name.startswith("state.")
                    else ToolEffect.EXTERNAL_SIDE_EFFECT
                ),
                outcome=outcome,
                observed_at=datetime.now(timezone.utc),
                output=output,
                source_refs=list(authority_refs),
                idempotency_key=idempotency_key,
                error_code=error_code,
            )
            finalized_at = datetime.now(timezone.utc)
            try:
                self.commit_journal.finalize(
                    reservation.model_copy(
                        update={
                            "state": "final",
                            "receipt": receipt,
                            "updated_at": finalized_at,
                        }
                    )
                )
            except Exception:
                return self._indeterminate_receipt(
                    reservation,
                    snapshot=snapshot,
                    error_code="CommitJournalFinalizeError",
                )
            return receipt.model_copy(deep=True)

    def get_commit_receipt(self, idempotency_key: str) -> ToolReceipt | None:
        entry = self.commit_journal.get(idempotency_key)
        if entry is None or entry.state != "final":
            return None
        return entry.receipt.model_copy(deep=True) if entry.receipt else None

    @staticmethod
    def _receipt_from_journal(
        entry: CommitJournalEntry,
        *,
        tool_name: str,
        input_hash: str,
        snapshot: FactSnapshot,
    ) -> ToolReceipt:
        if (
            entry.tool_name != tool_name
            or entry.input_hash != input_hash
            or entry.fact_snapshot_hash != snapshot.fact_snapshot_hash
        ):
            raise StaleStateError("idempotency key collision")
        if entry.state == "final" and entry.receipt is not None:
            return entry.receipt.model_copy(deep=True)
        return DeterministicCommitController._indeterminate_receipt(
            entry,
            snapshot=snapshot,
            error_code="IndeterminatePriorAttempt",
        )

    @staticmethod
    def _indeterminate_receipt(
        entry: CommitJournalEntry,
        *,
        snapshot: FactSnapshot,
        error_code: str,
    ) -> ToolReceipt:
        return ToolReceipt(
            tool_invocation_id=(
                f"commit:{stable_hash(entry.idempotency_key)[:16]}"
            ),
            tool_name=entry.tool_name,
            tool_version="v1",
            caller="commit_controller",
            fact_snapshot_id=snapshot.fact_snapshot_id,
            fact_snapshot_hash=snapshot.fact_snapshot_hash,
            input_hash=entry.input_hash,
            effect=(
                ToolEffect.STATE_WRITE
                if entry.tool_name.startswith("state.")
                else ToolEffect.EXTERNAL_SIDE_EFFECT
            ),
            outcome=InvocationOutcome.UNKNOWN,
            observed_at=entry.updated_at,
            output={},
            source_refs=list(entry.authority_refs),
            idempotency_key=entry.idempotency_key,
            error_code=error_code,
        )


def publication_postflight(
    draft: CommunicationDraft,
    *,
    accepted_evidence: AcceptedWorkProduct | None,
    accepted_care: AcceptedWorkProduct | None,
    safety: AcceptedWorkProduct | None,
    safety_required: bool,
    safety_target: AcceptedWorkProduct | None = None,
    reviewed_knowledge_refs: set[str] | None = None,
    reviewed_knowledge_payloads: dict[str, Any] | None = None,
    readiness_decisions: tuple[MetricReadinessDecision, ...] = (),
) -> None:
    reviewed_boundaries: list[str] = []
    for decision in readiness_decisions:
        if decision.response_mode != ResponseMode.DEGRADED:
            continue
        boundary = degraded_boundary_sentence(decision)
        if boundary not in draft.text:
            raise PublicationError(
                "degraded cold-start publication lacks reviewed boundary"
            )
        reviewed_boundaries.append(boundary)
    evidence_by_id = {
        item["claim_id"]: item
        for item in (
            accepted_evidence.payload.get("claims", [])
            if accepted_evidence
            else []
        )
    }
    if not set(draft.claim_refs).issubset(evidence_by_id):
        raise PublicationError("publication contains unsupported personal claims")
    care_by_id: dict[str, dict[str, Any]] = {}
    if accepted_care and accepted_care.payload.get("primary_action"):
        action = accepted_care.payload["primary_action"]
        care_by_id[action["candidate_id"]] = action
    if not set(draft.care_candidate_refs).issubset(care_by_id):
        raise PublicationError("publication changes Care candidate")
    bindings_by_ref: dict[str, list[Any]] = {}
    bound_numbers: set[str] = {
        token
        for boundary in reviewed_boundaries
        for token in _numeric_tokens(boundary)
    }
    reviewed_knowledge_refs = reviewed_knowledge_refs or set()
    reviewed_knowledge_payloads = reviewed_knowledge_payloads or {}
    for binding in draft.semantic_bindings:
        if binding.rendered_text not in draft.text:
            raise PublicationError(
                "publication semantic binding is absent from final text"
            )
        bindings_by_ref.setdefault(binding.source_ref, []).append(binding)
        if binding.source_kind == "evidence_claim":
            source = evidence_by_id.get(binding.source_ref)
        elif binding.source_kind == "care_candidate":
            source = care_by_id.get(binding.source_ref)
        else:
            if binding.source_ref not in reviewed_knowledge_refs:
                raise PublicationError(
                    "publication knowledge binding is not reviewed"
                )
            source = reviewed_knowledge_payloads.get(binding.source_ref)
            if source is None:
                raise PublicationError(
                    "publication knowledge binding lacks source payload"
                )
        if source is None:
            raise PublicationError("publication semantic source is unavailable")
        rendered_numbers = _numeric_tokens(binding.rendered_text)
        if set(binding.preserved_numbers) != rendered_numbers:
            raise PublicationError(
                "publication semantic binding omits rendered numbers"
            )
        if not rendered_numbers.issubset(_numeric_tokens(str(source))):
            raise PublicationError("publication changed an upstream number")
        bound_numbers.update(rendered_numbers)
    if not set(draft.claim_refs).issubset(bindings_by_ref):
        raise PublicationError("publication claim lacks semantic binding")
    if not set(draft.care_candidate_refs).issubset(bindings_by_ref):
        raise PublicationError("publication Care action lacks semantic binding")
    if not _numeric_tokens(draft.text).issubset(bound_numbers):
        raise PublicationError("publication contains an unbound number")
    if safety_required:
        target = safety_target or accepted_care or accepted_evidence
        if target is None:
            raise PublicationError("Safety target missing")
        require_safety_approval(target, safety)


__all__ = [
    "GOVERNANCE_VERSION",
    "PRODUCT_SAFETY_POLICY_VERSION",
    "AcceptanceError",
    "AcceptedWorkProduct",
    "CareActionCatalog",
    "CareActionDefinition",
    "CareDeliveryPolicy",
    "CareContextState",
    "CareTransitionEvent",
    "CommitJournal",
    "CommitJournalEntry",
    "ConfirmationError",
    "DeterministicCommitController",
    "InMemoryCareContextStore",
    "InMemoryCommitJournal",
    "InMemoryMemoryContextStore",
    "MemoryContextState",
    "MemoryItem",
    "PublicationError",
    "StaleStateError",
    "accept_care",
    "accept_communication",
    "accept_evidence",
    "accept_safety",
    "publication_postflight",
    "require_safety_approval",
    "safety_trigger_reasons",
]
