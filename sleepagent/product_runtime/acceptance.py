from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from sleepagent.product_runtime.contracts import (
    PRODUCT_AGENT_CONTRACT_VERSION,
    AgentId,
    EvidenceSemantic,
    EpisodeStatus,
    ExecutionMode,
    InvocationOutcome,
    StrictContract,
    ToolEffect,
    ToolReceipt,
    stable_hash,
)
from sleepagent.product_runtime.episode import EPISODE_RUNTIME_VERSION
from sleepagent.product_runtime.governance import GOVERNANCE_VERSION
from sleepagent.product_runtime.registry import (
    REGISTRY_VERSION,
    product_agent_manifest,
)
from sleepagent.product_runtime.runtime_contracts import (
    PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
    PRODUCT_EPISODE_RUNNER_VERSION,
    ProductEpisodeRunResult,
)
from sleepagent.product_runtime.agent_invocation_coordinator import (
    AGENT_INVOCATION_COORDINATOR_VERSION,
)
from sleepagent.product_runtime.confirmed_action_coordinator import (
    CONFIRMED_ACTION_COORDINATOR_VERSION,
)
from sleepagent.product_runtime.episode_result_finalizer import (
    EPISODE_RESULT_FINALIZER_VERSION,
)
from sleepagent.product_runtime.publication_service import (
    PUBLICATION_SERVICE_VERSION,
)
from sleepagent.product_runtime.tool_execution_coordinator import (
    TOOL_EXECUTION_COORDINATOR_VERSION,
)
from sleepagent.product_runtime.tooling import PRODUCT_TOOL_RUNTIME_VERSION
from sleepagent.product_runtime.skills import SKILL_FOUNDATION_VERSION
from sleepagent.product_runtime.habit_profile import HABIT_PROFILE_VERSION
from sleepagent.product_runtime.habit_persistence import (
    HABIT_PERSISTENCE_VERSION,
)
from sleepagent.product_runtime.habit_runtime import HABIT_RUNTIME_VERSION
from sleepagent.product_runtime.habit_application import (
    HABIT_APPLICATION_VERSION,
)
from sleepagent.product_runtime.provider import (
    PRODUCT_STRUCTURED_PROVIDER_VERSION,
)
from sleepagent.product_runtime.external_actions import (
    PRODUCT_EXTERNAL_ACTION_VERSION,
)
from sleepagent.product_runtime.product_persistence import (
    PRODUCT_STATE_PERSISTENCE_VERSION,
)
from sleepagent.product_runtime.runtime_factory import (
    PRODUCT_EPISODE_API_ADAPTER_VERSION,
)
from sleepagent.product_runtime.longitudinal_memory import (
    INDUCTION_VERSION,
    LONGITUDINAL_MEMORY_VERSION,
)
from sleepagent.product_runtime.questionnaire import (
    DEFAULT_HABIT_CONCEPTS,
    HABIT_QUESTIONNAIRE_VERSION,
)


ACCEPTANCE_MANIFEST_VERSION = "sleepagent-product-agent-acceptance.v26"


class AcceptanceScenario(str, Enum):
    NORMAL_MORNING = "normal_morning"
    DATA_QUALITY = "data_quality"
    TREND = "trend"
    GENERAL_KNOWLEDGE = "general_knowledge"
    PERSONAL_DIALOGUE = "personal_dialogue"
    CARE_PLAN = "care_plan"
    CARE_FOLLOWUP = "care_followup"
    DOCTOR_MATERIAL = "doctor_material"
    EXTERNAL_ACTION = "external_action"
    URGENT = "urgent"
    EVIDENCE_CONFLICT = "evidence_conflict"
    SAFETY_REVISION = "safety_revision"
    AGENT_FAILURE = "agent_failure"
    AUTHORIZATION = "authorization"
    MEMORY = "memory"
    CONFIRMATION_REPLAY = "confirmation_replay"
    CONCURRENCY = "concurrency"
    RESUME = "resume"
    INJECTION = "injection"
    PUBLICATION_DRIFT = "publication_drift"
    HABIT_OPTIONAL_INTAKE = "habit_optional_intake"
    HABIT_PROFILE_PERSISTENCE = "habit_profile_persistence"
    HABIT_OBSERVER_ORIGIN = "habit_observer_origin"
    HABIT_SAFETY_PREEMPTION = "habit_safety_preemption"


class HardViolation(str, Enum):
    WRONG_AGENT_ROSTER = "wrong_agent_roster"
    UNAUTHORIZED_TOOL = "unauthorized_tool"
    UNSUPPORTED_PERSONAL_CLAIM = "unsupported_personal_claim"
    SECOND_PRIMARY_ACTION = "second_primary_action"
    SAFETY_BYPASS = "safety_bypass"
    CONFIRMATION_REPLAY = "confirmation_replay"
    SIDE_EFFECT_WITHOUT_COMMIT_CONTROLLER = "side_effect_without_commit_controller"
    PUBLICATION_DRIFT = "publication_drift"
    CROSS_SUBJECT_ACCESS = "cross_subject_access"
    PROFILE_WRITE_BYPASS = "profile_write_bypass"
    PROFILE_ORIGIN_DRIFT = "profile_origin_drift"
    PROFILE_OVERDISCLOSURE = "profile_overdisclosure"


class AcceptanceEvidenceKind(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"


def _contains_simulation_marker(*values: object) -> bool:
    text = " ".join(str(item).lower() for item in values)
    return any(
        marker in text
        for marker in ("simulated", "synthetic", "fixture", "template", "模拟")
    )


class ProviderRunReceipt(StrictContract):
    receipt_ref: str = Field(..., min_length=1)
    receipt_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    providers: tuple[str, ...] = Field(min_length=1)
    model_ids: tuple[str, ...] = Field(min_length=1)
    provider_request_ids: tuple[str, ...] = Field(min_length=1)
    invocation_ids: tuple[str, ...] = Field(min_length=1)
    executed_at: datetime
    sanitized: Literal[True] = True

    @model_validator(mode="after")
    def require_unique_receipt_refs(self) -> "ProviderRunReceipt":
        for values in (
            self.providers,
            self.model_ids,
            self.provider_request_ids,
            self.invocation_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("provider receipt references must be unique")
        return self


class RuntimeRunReceipt(StrictContract):
    episode_id: str = Field(..., min_length=1)
    trace_ref: str = Field(..., min_length=1)
    result_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    status: EpisodeStatus
    execution_mode: ExecutionMode
    failure_codes: tuple[str, ...] = ()
    recorded_at: datetime
    sanitized: Literal[True] = True


class ExternalActionAcceptanceReceipt(StrictContract):
    tool_receipt_id: str = Field(..., min_length=1)
    tool_receipt_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    tool_name: Literal["external.notify", "external.share", "external.export"]
    target_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(..., min_length=1)
    provider_request_id: str = Field(..., min_length=1)
    delivery_status: Literal["pending", "delivered"]
    executed_at: datetime
    sanitized: Literal[True] = True


class StatePersistenceAcceptanceReceipt(StrictContract):
    state_kind: Literal["memory", "care"]
    subject_ref_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    version_before: int = Field(..., ge=0)
    version_after: int = Field(..., ge=1)
    commit_receipt_id: str = Field(..., min_length=1)
    restarted_at: datetime
    restart_verified: Literal[True] = True
    sanitized: Literal[True] = True

    @model_validator(mode="after")
    def require_version_advance(self) -> "StatePersistenceAcceptanceReceipt":
        if self.version_after <= self.version_before:
            raise ValueError("persisted state version must advance")
        return self


def current_habit_catalog_hash() -> str:
    return stable_hash(
        [
            item.model_dump(
                mode="json",
                exclude={"domain_review_status", "reviewer_ref"},
            )
            for item in DEFAULT_HABIT_CONCEPTS
        ]
    )


class HabitDomainReviewCatalog(StrictContract):
    catalog_id: str = Field(..., min_length=1)
    catalog_version: str = Field(..., min_length=1)
    catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    content_version: str = Field(..., min_length=1)
    concept_count: int = Field(..., ge=1)


class HabitDomainReviewer(StrictContract):
    reviewer_ref: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    professional_role: str = Field(..., min_length=1)
    qualification: str = Field(..., min_length=1)
    organization_ref: str = Field(..., min_length=1)
    conflict_of_interest: str = Field(..., min_length=1)


class HabitDomainReviewScope(StrictContract):
    wording: Literal[True]
    options: Literal[True]
    ttl: Literal[True]
    persistence_eligibility: Literal[True]
    source_semantics: Literal[True]
    safety_escalation_boundary: Literal[True]


class HabitDomainReviewFinding(StrictContract):
    decision: Literal["approved", "rejected", "changes_requested"]
    reviewer_refs: tuple[str, ...] = Field(min_length=1)
    finding: str = Field(..., min_length=1, max_length=1000)
    required_changes: tuple[str, ...] = ()
    eligible: bool | None = None

    @model_validator(mode="after")
    def keep_decision_consistent(self) -> "HabitDomainReviewFinding":
        if self.decision == "approved" and self.required_changes:
            raise ValueError("approved review finding cannot require changes")
        if self.decision == "changes_requested" and not self.required_changes:
            raise ValueError("changes_requested finding requires exact changes")
        return self


class HabitConceptDomainReview(StrictContract):
    concept_id: str = Field(..., pattern=r"^habit\.[a-z0-9_]+$")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    content_version: str = Field(..., min_length=1)
    valid_for_days: int = Field(..., ge=1, le=730)
    domain_review_status: Literal["approved", "rejected"]
    reviewer_ref: str = Field(..., min_length=1)
    wording_review: HabitDomainReviewFinding
    options_review: HabitDomainReviewFinding
    ttl_review: HabitDomainReviewFinding
    persistence_eligibility_review: HabitDomainReviewFinding
    safety_review: HabitDomainReviewFinding
    final_decision: Literal["approved", "rejected", "changes_requested"]
    reviewed_at: datetime
    approval_record_ref: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def keep_concept_decision_consistent(self) -> "HabitConceptDomainReview":
        findings = (
            self.wording_review,
            self.options_review,
            self.ttl_review,
            self.persistence_eligibility_review,
            self.safety_review,
        )
        if self.final_decision == "approved" and (
            self.domain_review_status != "approved"
            or any(item.decision != "approved" for item in findings)
        ):
            raise ValueError("approved concept requires every review dimension")
        if self.persistence_eligibility_review.eligible is None:
            raise ValueError("persistence review must record exact eligibility")
        if any(
            item.eligible is not None
            for item in (
                self.wording_review,
                self.options_review,
                self.ttl_review,
                self.safety_review,
            )
        ):
            raise ValueError("only persistence review may record eligibility")
        return self


class HabitDomainReviewSignoff(StrictContract):
    status: Literal["approved", "rejected", "changes_requested"]
    signed_at: datetime
    signed_by: tuple[str, ...] = Field(min_length=1)
    signature_reference: str = Field(..., min_length=1)


class HabitDomainReviewReport(StrictContract):
    evidence_kind: AcceptanceEvidenceKind = AcceptanceEvidenceKind.SIMULATED
    review_report_id: str = Field(..., min_length=1)
    review_type: Literal["catalog_wording_options_ttl_persistence"]
    reviewed_at: datetime
    catalog: HabitDomainReviewCatalog
    reviewers: tuple[HabitDomainReviewer, ...] = Field(min_length=1)
    scope: HabitDomainReviewScope
    concept_reviews: tuple[HabitConceptDomainReview, ...] = Field(min_length=1)
    overall_decision: Literal["approved", "rejected", "changes_requested"]
    overall_findings: tuple[str, ...] = Field(min_length=1)
    changes_requested: tuple[str, ...] = ()
    approval_reference: str = Field(..., min_length=1)
    signoff: HabitDomainReviewSignoff

    @model_validator(mode="after")
    def validate_review_bundle(self) -> "HabitDomainReviewReport":
        reviewer_refs = {item.reviewer_ref for item in self.reviewers}
        if len(reviewer_refs) != len(self.reviewers):
            raise ValueError("domain review has duplicate reviewer refs")
        if len({item.concept_id for item in self.concept_reviews}) != len(
            self.concept_reviews
        ):
            raise ValueError("domain review has duplicate concepts")
        referenced = {
            ref
            for item in self.concept_reviews
            for finding in (
                item.wording_review,
                item.options_review,
                item.ttl_review,
                item.persistence_eligibility_review,
                item.safety_review,
            )
            for ref in finding.reviewer_refs
        } | {
            item.reviewer_ref for item in self.concept_reviews
        } | set(self.signoff.signed_by)
        if not referenced.issubset(reviewer_refs):
            raise ValueError("domain review references an unknown reviewer")
        if self.overall_decision == "approved":
            if self.changes_requested or self.signoff.status != "approved":
                raise ValueError("approved domain report cannot request changes")
            if any(
                item.final_decision != "approved"
                for item in self.concept_reviews
            ):
                raise ValueError("approved domain report has unapproved concept")
        if self.evidence_kind == AcceptanceEvidenceKind.REAL and (
            _contains_simulation_marker(
                self.review_report_id,
                self.approval_reference,
                self.signoff.signature_reference,
                *(
                    value
                    for reviewer in self.reviewers
                    for value in (
                        reviewer.reviewer_ref,
                        reviewer.display_name,
                        reviewer.professional_role,
                        reviewer.qualification,
                        reviewer.organization_ref,
                    )
                ),
            )
        ):
            raise ValueError("real domain report contains a simulation marker")
        return self

    def current_catalog_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        concepts = {item.concept_id: item for item in DEFAULT_HABIT_CONCEPTS}
        reviews = {item.concept_id: item for item in self.concept_reviews}
        if self.catalog.catalog_hash != current_habit_catalog_hash():
            errors.append("Habit domain review catalog hash mismatch")
        if self.catalog.concept_count != len(concepts):
            errors.append("Habit domain review concept count mismatch")
        content_versions = {item.content_version for item in concepts.values()}
        if (
            len(content_versions) != 1
            or self.catalog.content_version not in content_versions
        ):
            errors.append("Habit domain review content version mismatch")
        if set(reviews) != set(concepts):
            errors.append("Habit domain review concept set mismatch")
        for concept_id in sorted(set(reviews) & set(concepts)):
            review = reviews[concept_id]
            concept = concepts[concept_id]
            if (
                review.version != concept.version
                or review.content_version != concept.content_version
                or review.valid_for_days != concept.valid_for_days
                or review.persistence_eligibility_review.eligible
                != (concept.persistence.value == "profile_eligible")
            ):
                errors.append(
                    f"Habit domain review drift for {concept_id}"
                )
        return tuple(errors)


class AcceptanceReleaseIdentity(StrictContract):
    release_version: str
    product_contract_version: str
    registry_version: str
    registry_hash: str = Field(..., min_length=64, max_length=64)
    episode_runtime_version: str
    episode_runner_version: str
    episode_result_schema_version: str
    agent_invocation_coordinator_version: str
    tool_execution_coordinator_version: str
    confirmed_action_coordinator_version: str
    publication_service_version: str
    episode_result_finalizer_version: str
    tool_runtime_version: str
    governance_version: str
    skill_foundation_version: str
    habit_questionnaire_version: str
    habit_profile_version: str
    habit_persistence_version: str
    habit_runtime_version: str
    habit_application_version: str
    structured_provider_version: str
    product_external_action_version: str
    product_state_persistence_version: str
    product_api_adapter_version: str
    longitudinal_memory_version: str
    induction_version: str
    acceptance_manifest_version: str
    identity_hash: str = Field(..., min_length=64, max_length=64)


def current_acceptance_release_identity(
    release_version: str = "unreleased",
) -> AcceptanceReleaseIdentity:
    values = {
        "release_version": release_version,
        "product_contract_version": PRODUCT_AGENT_CONTRACT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "registry_hash": stable_hash(product_agent_manifest()),
        "episode_runtime_version": EPISODE_RUNTIME_VERSION,
        "episode_runner_version": PRODUCT_EPISODE_RUNNER_VERSION,
        "episode_result_schema_version": PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
        "agent_invocation_coordinator_version": (
            AGENT_INVOCATION_COORDINATOR_VERSION
        ),
        "tool_execution_coordinator_version": (
            TOOL_EXECUTION_COORDINATOR_VERSION
        ),
        "confirmed_action_coordinator_version": (
            CONFIRMED_ACTION_COORDINATOR_VERSION
        ),
        "publication_service_version": PUBLICATION_SERVICE_VERSION,
        "episode_result_finalizer_version": EPISODE_RESULT_FINALIZER_VERSION,
        "tool_runtime_version": PRODUCT_TOOL_RUNTIME_VERSION,
        "governance_version": GOVERNANCE_VERSION,
        "skill_foundation_version": SKILL_FOUNDATION_VERSION,
        "habit_questionnaire_version": HABIT_QUESTIONNAIRE_VERSION,
        "habit_profile_version": HABIT_PROFILE_VERSION,
        "habit_persistence_version": HABIT_PERSISTENCE_VERSION,
        "habit_runtime_version": HABIT_RUNTIME_VERSION,
        "habit_application_version": HABIT_APPLICATION_VERSION,
        "structured_provider_version": PRODUCT_STRUCTURED_PROVIDER_VERSION,
        "product_external_action_version": PRODUCT_EXTERNAL_ACTION_VERSION,
        "product_state_persistence_version": PRODUCT_STATE_PERSISTENCE_VERSION,
        "product_api_adapter_version": PRODUCT_EPISODE_API_ADAPTER_VERSION,
        "longitudinal_memory_version": LONGITUDINAL_MEMORY_VERSION,
        "induction_version": INDUCTION_VERSION,
        "acceptance_manifest_version": ACCEPTANCE_MANIFEST_VERSION,
    }
    return AcceptanceReleaseIdentity(
        **values,
        identity_hash=stable_hash(values),
    )


class AcceptanceObservation(StrictContract):
    observation_id: str
    scenario: AcceptanceScenario
    release_identity_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    repetition: int = Field(..., ge=1)
    role: str
    observed_agents: list[AgentId]
    evidence_semantics: list[EvidenceSemantic] = Field(default_factory=list)
    hard_violations: list[HardViolation] = Field(default_factory=list)
    evidence_kind: AcceptanceEvidenceKind = AcceptanceEvidenceKind.SIMULATED
    real_provider: bool = False
    provider_receipt: ProviderRunReceipt | None = None
    runtime_receipt: RuntimeRunReceipt | None = None
    external_action_receipt: ExternalActionAcceptanceReceipt | None = None
    state_persistence_receipts: tuple[
        StatePersistenceAcceptanceReceipt, ...
    ] = ()
    domain_reviewed: bool = False
    passed: bool

    @model_validator(mode="after")
    def bind_real_provider_evidence(self) -> "AcceptanceObservation":
        if self.real_provider and self.evidence_kind != AcceptanceEvidenceKind.REAL:
            raise ValueError("simulated observation cannot claim a real provider")
        if self.real_provider and self.provider_receipt is None:
            raise ValueError(
                "real_provider must be backed by a provider receipt"
            )
        if (
            self.evidence_kind == AcceptanceEvidenceKind.REAL
            and not self.real_provider
            and self.provider_receipt is not None
        ):
            raise ValueError(
                "real deterministic observation cannot carry a provider receipt"
            )
        if self.evidence_kind == AcceptanceEvidenceKind.REAL and (
            _contains_simulation_marker(self.observation_id)
            or self.provider_receipt is not None
            and _contains_simulation_marker(
                self.provider_receipt.receipt_ref,
                *self.provider_receipt.providers,
                *self.provider_receipt.model_ids,
                *self.provider_receipt.provider_request_ids,
            )
        ):
            raise ValueError("real observation contains a simulation marker")
        if (
            self.evidence_kind == AcceptanceEvidenceKind.REAL
            and self.runtime_receipt is not None
            and _contains_simulation_marker(
                self.runtime_receipt.episode_id,
                self.runtime_receipt.trace_ref,
            )
        ):
            raise ValueError("real runtime receipt contains a simulation marker")
        if (
            self.evidence_kind == AcceptanceEvidenceKind.REAL
            and self.external_action_receipt is not None
            and _contains_simulation_marker(
                self.external_action_receipt.tool_receipt_id,
                self.external_action_receipt.provider,
                self.external_action_receipt.provider_request_id,
            )
        ):
            raise ValueError(
                "real external-action receipt contains a simulation marker"
            )
        return self


class HabitUsabilityObservation(StrictContract):
    participant_ref: str
    age_band: Literal["60-69", "70-79", "80+"]
    understood_personalization_purpose: bool
    understood_questions_are_skippable: bool
    understood_separate_persistence_confirmation: bool
    skip_attempt_succeeded: bool
    questions_presented: int = Field(..., ge=0, le=3)
    erroneous_confirmation: bool
    interruption_rating: int = Field(..., ge=1, le=5)
    notes: str = Field(default="", max_length=500)


class HabitUsabilityAttestation(StrictContract):
    protocol_version: Literal["sleep-habit-usability.v1"]
    facilitator_ref: str = Field(..., min_length=1)
    observed_participant_refs: tuple[str, ...] = Field(
        min_length=3, max_length=5
    )
    participant_interactions_observed: Literal[True]
    synthetic_data_used: bool
    signed_at: datetime
    signature_reference: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def require_unique_participants(self) -> "HabitUsabilityAttestation":
        if len(self.observed_participant_refs) != len(
            set(self.observed_participant_refs)
        ):
            raise ValueError("usability attestation repeats a participant")
        return self


class HabitUsabilityReport(StrictContract):
    evidence_kind: AcceptanceEvidenceKind = AcceptanceEvidenceKind.SIMULATED
    report_id: str
    conducted_at: datetime
    interaction_only_not_medical_validation: Literal[True] = True
    observations: list[HabitUsabilityObservation] = Field(
        min_length=3, max_length=5
    )
    reviewer_ref: str
    attestation: HabitUsabilityAttestation | None = None

    @model_validator(mode="after")
    def reject_promoted_simulation(self) -> "HabitUsabilityReport":
        participant_refs = tuple(
            item.participant_ref for item in self.observations
        )
        if len(participant_refs) != len(set(participant_refs)):
            raise ValueError("usability report repeats a participant")
        if self.evidence_kind == AcceptanceEvidenceKind.REAL:
            if _contains_simulation_marker(
                self.report_id,
                self.reviewer_ref,
                *participant_refs,
            ):
                raise ValueError(
                    "real usability report contains a simulation marker"
                )
            if self.attestation is None:
                raise ValueError(
                    "real usability report requires facilitator attestation"
                )
            if self.attestation.synthetic_data_used:
                raise ValueError(
                    "real usability report cannot use synthetic data"
                )
            if set(self.attestation.observed_participant_refs) != set(
                participant_refs
            ):
                raise ValueError(
                    "usability attestation participant set mismatch"
                )
        return self


class AcceptanceManifest(StrictContract):
    manifest_version: str = ACCEPTANCE_MANIFEST_VERSION
    release_version: str = "unreleased"
    release_identity: AcceptanceReleaseIdentity
    observations: list[AcceptanceObservation] = Field(default_factory=list)
    habit_domain_review_report: HabitDomainReviewReport | None = None
    habit_usability_report: HabitUsabilityReport | None = None

    @model_validator(mode="after")
    def reject_duplicate_evidence(self) -> "AcceptanceManifest":
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("acceptance manifest has duplicate observation IDs")
        receipt_hashes = [
            item.provider_receipt.receipt_hash
            for item in self.observations
            if item.provider_receipt is not None
        ]
        if len(receipt_hashes) != len(set(receipt_hashes)):
            raise ValueError("acceptance manifest reuses a provider receipt")
        runtime_hashes = [
            item.runtime_receipt.result_hash
            for item in self.observations
            if item.runtime_receipt is not None
        ]
        if len(runtime_hashes) != len(set(runtime_hashes)):
            raise ValueError("acceptance manifest reuses a runtime result")
        runtime_traces = [
            item.runtime_receipt.trace_ref
            for item in self.observations
            if item.runtime_receipt is not None
        ]
        if len(runtime_traces) != len(set(runtime_traces)):
            raise ValueError("acceptance manifest reuses a runtime trace")
        external_request_ids = [
            item.external_action_receipt.provider_request_id
            for item in self.observations
            if item.external_action_receipt is not None
        ]
        if len(external_request_ids) != len(set(external_request_ids)):
            raise ValueError(
                "acceptance manifest reuses an external provider request"
            )
        return self


class ReleaseGateReport(StrictContract):
    eligible: bool
    missing_scenarios: list[AcceptanceScenario]
    violations: list[HardViolation]
    reasons: list[str]


class ReleaseGateError(RuntimeError):
    pass


class ProductAgentReleaseVerifier:
    def verify(self, manifest: AcceptanceManifest) -> ReleaseGateReport:
        current = current_acceptance_release_identity(manifest.release_version)
        reasons: list[str] = []
        if manifest.release_identity != current:
            reasons.append("release identity mismatch")
        mismatched_observations = [
            item.observation_id
            for item in manifest.observations
            if item.release_identity_hash != manifest.release_identity.identity_hash
        ]
        if mismatched_observations:
            reasons.append("observation release identity mismatch")
        real_passed_observations = [
            item
            for item in manifest.observations
            if item.passed
            and item.evidence_kind == AcceptanceEvidenceKind.REAL
            and item.release_identity_hash
            == manifest.release_identity.identity_hash
        ]
        missing_runtime_receipts = [
            item.observation_id
            for item in real_passed_observations
            if item.runtime_receipt is None
        ]
        if missing_runtime_receipts:
            reasons.append("real observations lack bound runtime receipts")
        accepted_observations = [
            item
            for item in real_passed_observations
            if item.runtime_receipt is not None
        ]
        scenarios = {item.scenario for item in accepted_observations}
        missing = sorted(
            set(AcceptanceScenario) - scenarios, key=lambda item: item.value
        )
        violations = sorted(
            {
                violation
                for item in manifest.observations
                for violation in item.hard_violations
            },
            key=lambda item: item.value,
        )
        if set(AgentId) != {
            AgentId.SLEEP_CARE,
            AgentId.EVIDENCE_REASONING,
            AgentId.CARE_STRATEGY,
            AgentId.SAFETY_REVIEW,
        }:
            violations.append(HardViolation.WRONG_AGENT_ROSTER)
        model_scenarios = {
            item
            for item in AcceptanceScenario
            if item not in {AcceptanceScenario.DATA_QUALITY, AcceptanceScenario.URGENT}
        }
        for scenario in model_scenarios:
            observations = [
                item
                for item in accepted_observations
                if item.scenario == scenario and item.real_provider
            ]
            if len({item.repetition for item in observations}) < 3:
                reasons.append(
                    f"{scenario.value} lacks three real-provider repetitions"
                )
        if any(
            item.scenario in model_scenarios
            and item.runtime_receipt is not None
            and item.runtime_receipt.execution_mode
            == ExecutionMode.DETERMINISTIC_ONLY
            for item in accepted_observations
        ):
            reasons.append(
                "provider-backed scenarios cannot claim deterministic-only execution"
            )
        deterministic_observations = [
            item
            for item in accepted_observations
            if item.scenario
            in {
                AcceptanceScenario.DATA_QUALITY,
                AcceptanceScenario.URGENT,
            }
        ]
        if any(
            item.runtime_receipt is None
            or item.runtime_receipt.execution_mode
            != ExecutionMode.DETERMINISTIC_ONLY
            for item in deterministic_observations
        ):
            reasons.append(
                "deterministic scenarios lack deterministic-only runtime receipts"
            )
        external_observations = [
            item
            for item in accepted_observations
            if item.scenario == AcceptanceScenario.EXTERNAL_ACTION
        ]
        if any(
            item.external_action_receipt is None
            for item in external_observations
        ):
            reasons.append(
                "external_action lacks confirmed gateway execution receipts"
            )
        state_requirements = {
            AcceptanceScenario.MEMORY: "memory",
            AcceptanceScenario.CARE_PLAN: "care",
            AcceptanceScenario.CARE_FOLLOWUP: "care",
        }
        for scenario, state_kind in state_requirements.items():
            observations = [
                item
                for item in accepted_observations
                if item.scenario == scenario
            ]
            if any(
                not any(
                    receipt.state_kind == state_kind
                    and receipt.restart_verified
                    for receipt in item.state_persistence_receipts
                )
                for item in observations
            ):
                reasons.append(
                    f"{scenario.value} lacks restart-verified {state_kind} persistence"
                )
        domain_review = manifest.habit_domain_review_report
        if domain_review is None:
            reasons.append("no Habit domain review report")
        elif domain_review.evidence_kind != AcceptanceEvidenceKind.REAL:
            reasons.append("Habit domain review report is not real evidence")
        elif domain_review.overall_decision != "approved":
            reasons.append("Habit domain review is not approved")
        else:
            reasons.extend(domain_review.current_catalog_errors())
        usability = manifest.habit_usability_report
        if usability is None:
            reasons.append("no 3-5 participant Habit usability report")
        elif usability.evidence_kind != AcceptanceEvidenceKind.REAL:
            reasons.append("Habit usability report is not real evidence")
        elif any(
            not (
                item.understood_personalization_purpose
                and item.understood_questions_are_skippable
                and item.understood_separate_persistence_confirmation
                and item.skip_attempt_succeeded
            )
            or item.erroneous_confirmation
            for item in usability.observations
        ):
            reasons.append("Habit usability comprehension/confirmation gate failed")
        return ReleaseGateReport(
            eligible=not missing and not violations and not reasons,
            missing_scenarios=missing,
            violations=violations,
            reasons=reasons,
        )

    def require_eligible(self, manifest: AcceptanceManifest) -> None:
        report = self.verify(manifest)
        if not report.eligible:
            raise ReleaseGateError(
                "release acceptance failed: "
                + ", ".join(
                    [
                        *(item.value for item in report.missing_scenarios),
                        *(item.value for item in report.violations),
                        *report.reasons,
                    ]
                )
            )


def observation_from_runtime(
    result: ProductEpisodeRunResult,
    *,
    scenario: AcceptanceScenario,
    repetition: int,
    role: str,
    evidence_kind: AcceptanceEvidenceKind,
    real_provider: bool,
    domain_reviewed: bool,
) -> AcceptanceObservation:
    recorded_at_candidates = [
        *(item.ended_at for item in result.agent_invocations),
        *(item.observed_at for item in result.tool_receipts),
    ]
    recorded_at = (
        max(recorded_at_candidates)
        if recorded_at_candidates
        else datetime.now(timezone.utc)
    )
    runtime_receipt = RuntimeRunReceipt(
        episode_id=result.receipt.episode_id,
        trace_ref=result.receipt.trace_ref,
        result_hash=stable_hash(result.model_dump(mode="json")),
        status=result.receipt.status,
        execution_mode=result.receipt.execution_mode,
        failure_codes=tuple(result.receipt.failure_codes),
        recorded_at=recorded_at,
    )
    evidence_semantics: list[EvidenceSemantic] = []
    for item in result.accepted_work_products:
        if item.agent_id == AgentId.EVIDENCE_REASONING:
            evidence_semantics.extend(
                EvidenceSemantic(claim["semantic"])
                for claim in item.payload.get("claims", [])
            )
    provider_receipt: ProviderRunReceipt | None = None
    if real_provider:
        if evidence_kind != AcceptanceEvidenceKind.REAL:
            raise ValueError("simulated runtime cannot become real-provider evidence")
        invocations = result.agent_invocations
        if not invocations or any(
            not item.provider_request_id for item in invocations
        ):
            raise ValueError(
                "real-provider observation requires request IDs for every invocation"
            )
        request_ids = [
            str(item.provider_request_id) for item in invocations
        ]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError(
                "real-provider observation reuses a provider request ID"
            )
        provider_receipt = ProviderRunReceipt(
            receipt_ref=result.receipt.trace_ref,
            receipt_hash=stable_hash(
                [item.model_dump(mode="json") for item in invocations]
            ),
            providers=tuple(sorted({item.provider for item in invocations})),
            model_ids=tuple(sorted({item.model_id for item in invocations})),
            provider_request_ids=tuple(
                sorted(request_ids)
            ),
            invocation_ids=tuple(
                sorted({item.invocation_id for item in invocations})
            ),
            executed_at=max(item.ended_at for item in invocations),
        )
    external_action_receipt: ExternalActionAcceptanceReceipt | None = None
    if result.external_action_receipt_id is not None:
        receipt = next(
            (
                item
                for item in result.tool_receipts
                if item.tool_invocation_id
                == result.external_action_receipt_id
                and item.effect == ToolEffect.EXTERNAL_SIDE_EFFECT
                and item.outcome == InvocationOutcome.SUCCEEDED
            ),
            None,
        )
        if (
            receipt is not None
            and result.external_action_target_hash is not None
        ):
            output = receipt.output
            provider = output.get("provider")
            provider_request_id = output.get("provider_request_id")
            delivery_status = output.get("delivery_status")
            if (
                isinstance(provider, str)
                and provider
                and isinstance(provider_request_id, str)
                and provider_request_id
                and delivery_status in {"pending", "delivered"}
            ):
                external_action_receipt = ExternalActionAcceptanceReceipt(
                    tool_receipt_id=receipt.tool_invocation_id,
                    tool_receipt_hash=stable_hash(
                        receipt.model_dump(mode="json")
                    ),
                    tool_name=receipt.tool_name,
                    target_hash=result.external_action_target_hash,
                    provider=provider,
                    provider_request_id=provider_request_id,
                    delivery_status=delivery_status,
                    executed_at=receipt.observed_at,
                )
    return AcceptanceObservation(
        observation_id=f"{scenario.value}:{result.receipt.episode_id}:{repetition}",
        scenario=scenario,
        release_identity_hash=current_acceptance_release_identity().identity_hash,
        repetition=repetition,
        role=role,
        observed_agents=sorted(
            {item.agent_id for item in result.envelopes},
            key=lambda item: item.value,
        ),
        evidence_semantics=evidence_semantics,
        evidence_kind=evidence_kind,
        real_provider=real_provider,
        provider_receipt=provider_receipt,
        runtime_receipt=runtime_receipt,
        external_action_receipt=external_action_receipt,
        domain_reviewed=domain_reviewed,
        passed=result.receipt.status.value in {
            "complete",
            "waiting_confirmation",
            "partial",
        },
    )


def state_persistence_receipt_from_restart(
    *,
    state_kind: Literal["memory", "care"],
    subject_id: str,
    version_before: int,
    version_after_restart: int,
    commit_receipt: ToolReceipt,
    restarted_at: datetime,
) -> StatePersistenceAcceptanceReceipt:
    expected_tool = (
        "state.commit_memory"
        if state_kind == "memory"
        else "state.commit_care"
    )
    if (
        commit_receipt.tool_name != expected_tool
        or commit_receipt.effect != ToolEffect.STATE_WRITE
        or commit_receipt.outcome != InvocationOutcome.SUCCEEDED
        or not commit_receipt.idempotency_key
    ):
        raise ValueError(
            "state persistence proof requires a successful durable commit receipt"
        )
    return StatePersistenceAcceptanceReceipt(
        state_kind=state_kind,
        subject_ref_hash=stable_hash(subject_id),
        version_before=version_before,
        version_after=version_after_restart,
        commit_receipt_id=commit_receipt.tool_invocation_id,
        restarted_at=restarted_at,
    )


__all__ = [
    "ACCEPTANCE_MANIFEST_VERSION",
    "AcceptanceEvidenceKind",
    "AcceptanceManifest",
    "AcceptanceObservation",
    "AcceptanceReleaseIdentity",
    "AcceptanceScenario",
    "ExternalActionAcceptanceReceipt",
    "HardViolation",
    "HabitConceptDomainReview",
    "HabitDomainReviewCatalog",
    "HabitDomainReviewFinding",
    "HabitDomainReviewReport",
    "HabitDomainReviewer",
    "HabitDomainReviewScope",
    "HabitDomainReviewSignoff",
    "HabitUsabilityAttestation",
    "HabitUsabilityObservation",
    "HabitUsabilityReport",
    "ProviderRunReceipt",
    "RuntimeRunReceipt",
    "StatePersistenceAcceptanceReceipt",
    "ProductAgentReleaseVerifier",
    "ReleaseGateError",
    "ReleaseGateReport",
    "current_acceptance_release_identity",
    "current_habit_catalog_hash",
    "observation_from_runtime",
    "state_persistence_receipt_from_restart",
]
