from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    MemoryChangeCandidate,
    StrictContract,
    stable_hash,
)


class MemoryChangeRequest(StrictContract):
    operation: Literal["create", "replace", "expire", "forget"] = "create"
    target_candidate_id: str | None = Field(default=None, min_length=1)
    concept_id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9_.-]{1,159}$",
    )
    memory_type: Literal[
        "preference",
        "routine",
        "environment",
        "communication_preference",
    ]
    value_schema_id: Literal[
        "bounded_string.v1",
        "boolean.v1",
        "number.v1",
        "enum.v1",
    ]
    value_schema_version: str = "1"
    typed_value: Any
    source_ref: str = Field(..., min_length=1)
    provenance_type: Literal[
        "elder_confirmed",
        "authorized_observer",
        "accepted_evidence",
    ] = "elder_confirmed"
    sensitivity_class: Literal["personal", "sensitive_personal"] = "personal"
    explicit_user_authorization: bool = False

    @model_validator(mode="after")
    def bind_operation_target(self) -> "MemoryChangeRequest":
        if self.operation == "create" and self.target_candidate_id is not None:
            raise ValueError("create cannot specify target_candidate_id")
        if self.operation != "create" and self.target_candidate_id is None:
            raise ValueError(
                f"{self.operation} requires target_candidate_id"
            )
        return self


class MemoryCapabilityInput(StrictContract):
    subject_id: str | None = Field(default=None, min_length=1)
    accepted_evidence_refs: tuple[str, ...] = ()
    risk_level: Literal[
        "info", "watch", "uncertain", "escalate", "urgent_boundary"
    ]
    data_quality_status: str = Field(..., min_length=1)
    preference_changes: tuple[MemoryChangeRequest, ...] = ()
    trend_analysis_refs: tuple[str, ...] = ()
    care_event_refs: tuple[str, ...] = ()


class MemoryCapabilityRouting(StrictContract):
    memory_change_candidates: list[MemoryChangeCandidate] = Field(
        default_factory=list
    )
    analysis_artifact_refs: list[str] = Field(default_factory=list)
    care_state_event_refs: list[str] = Field(default_factory=list)
    rejected_reasons: list[str] = Field(default_factory=list)
    write_performed: Literal[False] = False


class SleepCareMemorySkill:
    """Route memory-like inputs without turning storage into an Agent.

    Preference changes become governed candidates.  Trend calculations remain
    versioned analysis artifacts and Care events remain business state/events;
    neither is copied into generic long-term profile Memory.
    """

    owner = AgentId.SLEEP_CARE
    skill_id = "propose_memory_change"
    skill_version = "1.0.0"

    def route(self, values: MemoryCapabilityInput) -> MemoryCapabilityRouting:
        if values.risk_level == "urgent_boundary":
            return MemoryCapabilityRouting(
                rejected_reasons=["urgent_boundary_excludes_memory_induction"]
            )
        if values.data_quality_status == "unusable":
            return MemoryCapabilityRouting(
                rejected_reasons=["unusable_data_excludes_memory_induction"]
            )
        if values.subject_id is None:
            return MemoryCapabilityRouting(
                rejected_reasons=["subject_id_missing"]
            )
        accepted_evidence_refs = tuple(
            dict.fromkeys(
                ref for ref in values.accepted_evidence_refs if ref
            )
        )
        if not accepted_evidence_refs:
            return MemoryCapabilityRouting(
                rejected_reasons=["accepted_evidence_refs_missing"]
            )

        candidates: list[MemoryChangeCandidate] = []
        rejected: list[str] = []
        for request in values.preference_changes:
            if request.source_ref.lower().startswith(
                (
                    "raw:",
                    "raw-event:",
                    "raw_ingress:",
                    "raw-inbox:",
                    "radar-raw:",
                    "vendor-event:",
                )
            ):
                rejected.append(
                    f"raw_source_not_memory_eligible:{request.concept_id}"
                )
                continue
            if (
                request.provenance_type == "accepted_evidence"
                and request.source_ref not in accepted_evidence_refs
            ):
                rejected.append(
                    "accepted_evidence_source_not_bound:"
                    f"{request.concept_id}"
                )
                continue
            material = {
                "subject_id": values.subject_id,
                **request.model_dump(mode="json"),
            }
            candidates.append(
                MemoryChangeCandidate(
                    candidate_id=(
                        request.target_candidate_id
                        or f"memory:{stable_hash(material)[:24]}"
                    ),
                    operation=request.operation,
                    subject_id=values.subject_id,
                    memory_type=request.memory_type,
                    concept_id=request.concept_id,
                    value_schema_id=request.value_schema_id,
                    value_schema_version=request.value_schema_version,
                    typed_value=request.typed_value,
                    provenance_type=request.provenance_type,
                    source_ref=request.source_ref,
                    sensitivity_class=request.sensitivity_class,
                    allowed_roles=(
                        AgentId.SLEEP_CARE,
                        AgentId.EVIDENCE_REASONING,
                    ),
                    allowed_purposes=(
                        "personal_evidence_context",
                        "explicit_memory_review",
                        "explicit_memory_change",
                        "explicit_memory_forget",
                    ),
                    explicit_user_authorization=(
                        request.explicit_user_authorization
                    ),
                    confirmation_required=True,
                )
            )
        return MemoryCapabilityRouting(
            memory_change_candidates=candidates,
            analysis_artifact_refs=list(
                dict.fromkeys(values.trend_analysis_refs)
            ),
            care_state_event_refs=list(dict.fromkeys(values.care_event_refs)),
            rejected_reasons=rejected,
        )


__all__ = [
    "MemoryCapabilityInput",
    "MemoryCapabilityRouting",
    "MemoryChangeRequest",
    "SleepCareMemorySkill",
]
