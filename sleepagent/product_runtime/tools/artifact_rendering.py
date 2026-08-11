from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from sleepagent.product_runtime.contracts import (
    AgentId,
    EvidencePacket as ProductEvidencePacket,
    StrictContract,
    stable_hash,
)
from sleepagent.product_runtime.reports.rendering import build_role_report_templates
from sleepagent.product_runtime.schemas import (
    ContextPacket,
    EvidenceLedger,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)


ARTIFACT_RENDERING_TOOL_VERSION = "sleepagent-artifact-rendering-tool.v1"

AudienceRole: TypeAlias = Literal["elder", "family", "doctor"]

ROLE_REPORT_SAFETY_NOTICES = (
    "本报告由 AI 辅助整理，内容来自结构化证据。",
    "本报告仅用于睡眠健康观察。",
    "本报告不构成临床诊断或医疗建议。",
    "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
)

_RISK_RANK = {
    RiskLevel.INFO: 0,
    RiskLevel.UNCERTAIN: 1,
    RiskLevel.WATCH: 2,
    RiskLevel.ESCALATE: 3,
    RiskLevel.URGENT_BOUNDARY: 4,
}


class ArtifactRenderRequest(StrictContract):
    """Exact deterministic inputs for one audience-specific report view."""

    context: ContextPacket
    evidence_ledger: EvidenceLedger
    audience_role: AudienceRole

    @model_validator(mode="after")
    def require_exact_context_ledger(self) -> "ArtifactRenderRequest":
        embedded = self.context.evidence_packet.evidence_ledger
        if embedded is None or embedded != self.evidence_ledger:
            raise ValueError(
                "Artifact rendering requires the exact EvidenceLedger from Context."
            )
        if self.context.task_context.task_id != self.evidence_ledger.task_id:
            raise ValueError("Context and EvidenceLedger task identities differ.")
        rag = self.context.rag_context
        if any(
            (
                rag.chunk_ids,
                rag.citation_ids,
                rag.snippets,
                rag.caveats,
                rag.source_metadata,
            )
        ):
            raise ValueError(
                "Artifact RAG Context requires an exact accepted Knowledge "
                "receipt binding."
            )
        if self.evidence_ledger.review_status is not ReviewStatus.REVIEWED:
            raise ValueError("Artifact rendering requires a reviewed EvidenceLedger.")
        if any(
            claim.review_status is not ReviewStatus.REVIEWED
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact rendering requires reviewed claims.")
        if any(
            claim.task_id != self.evidence_ledger.task_id
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact claim task identity differs from its ledger.")
        if any(
            claim.generated_by != AgentId.EVIDENCE_REASONING.value
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError(
                "Artifact claims require the canonical EvidenceReasoning owner."
            )
        if self.evidence_ledger.raw_evidence_refs:
            raise ValueError("Artifact rendering cannot consume raw evidence refs.")
        canonical_refs = set(self.evidence_ledger.canonical_evidence_refs)
        if any(
            not set(claim.evidence_refs).issubset(canonical_refs)
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact claims exceed canonical Evidence refs.")
        if any(
            entry.evidence_ref is not None
            and entry.evidence_ref not in canonical_refs
            for entry in self.evidence_ledger.questionnaire_entries
        ):
            raise ValueError("Artifact questionnaire exceeds canonical Evidence refs.")
        if any(
            item.review_status is not ReviewStatus.REVIEWED
            for item in self.evidence_ledger.supplementary_documents
        ):
            raise ValueError("Artifact supplementary documents must be reviewed.")
        declared_risk = RiskLevel(
            str(
                self.evidence_ledger.derived_metrics.get(
                    "risk_level",
                    RiskLevel.INFO.value,
                )
            )
        )
        claim_risk = max(
            (claim.risk_level for claim in self.evidence_ledger.claims),
            key=lambda item: _RISK_RANK[item],
            default=RiskLevel.INFO,
        )
        if _RISK_RANK[declared_risk] < _RISK_RANK[claim_risk]:
            raise ValueError("Artifact risk is below the reviewed claim risk floor.")
        return self


class ArtifactRenderResult(StrictContract):
    """Read-only rendering result; publication remains a Runtime responsibility."""

    tool_version: str = ARTIFACT_RENDERING_TOOL_VERSION
    audience_role: AudienceRole
    artifact: RoleReportArtifact
    source_refs: list[str]
    committed: Literal[False] = False
    exported: Literal[False] = False

    @model_validator(mode="after")
    def bind_result_to_one_audience(self) -> "ArtifactRenderResult":
        if self.artifact.role != self.audience_role:
            raise ValueError("Rendered artifact audience mismatch.")
        if self.source_refs != self.artifact.source_refs:
            raise ValueError("Render source_refs must come from the rendered artifact.")
        return self


class ProductArtifactBasisRequest(StrictContract):
    """Accepted Product Evidence used to prepare one role-material basis."""

    episode_id: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_packet: ProductEvidencePacket
    audience_role: AudienceRole

    @model_validator(mode="after")
    def bind_accepted_evidence(self) -> "ProductArtifactBasisRequest":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "artifact Evidence reference does not bind its accepted hash"
            )
        return self


class ProductArtifactBasisResult(StrictContract):
    """Prepared, non-published material basis for SleepCare expression.

    This result deliberately does not claim that a user-facing artifact has
    already been rendered.  SleepCare's versioned role-material Skill owns the
    final expression; publication remains a Runtime responsibility.
    """

    schema_version: Literal["product_artifact_basis.v1"] = (
        "product_artifact_basis.v1"
    )
    artifact_basis_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    audience_role: AudienceRole
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., min_length=64, max_length=64)
    claim_refs: list[str] = Field(max_length=30)
    evidence_refs: list[str] = Field(max_length=49)
    source_refs: list[str] = Field(max_length=50)
    basis_prepared: Literal[True] = True
    rendered: Literal[False] = False
    committed: Literal[False] = False
    exported: Literal[False] = False

    @model_validator(mode="after")
    def bind_source_projection(self) -> "ProductArtifactBasisResult":
        if self.source_refs != [
            self.accepted_evidence_ref,
            *self.evidence_refs,
        ]:
            raise ValueError(
                "artifact basis sources must bind accepted Evidence exactly"
            )
        return self


class ArtifactRenderingTool:
    """Render one role artifact from an exact Context/EvidenceLedger pair.

    The provider-neutral renderer owns the deterministic artifact structure.
    This Tool selects one requested audience and returns it without persistence,
    export, publication, model invocation, or Agent identity.
    """

    def render(self, request: ArtifactRenderRequest) -> ArtifactRenderResult:
        if type(request) is not ArtifactRenderRequest:
            raise TypeError("ArtifactRenderingTool requires ArtifactRenderRequest")

        risk = RiskLevel(
            str(
                request.evidence_ledger.derived_metrics.get(
                    "risk_level",
                    RiskLevel.INFO.value,
                )
            )
        )
        templates = build_role_report_templates(
            context=request.context,
            ledger=request.evidence_ledger,
            risk=risk,
            safety_notices=list(ROLE_REPORT_SAFETY_NOTICES),
        )
        artifact = next(
            item for item in templates if item.role == request.audience_role
        )
        return ArtifactRenderResult(
            audience_role=request.audience_role,
            artifact=artifact,
            source_refs=list(artifact.source_refs),
        )

    def prepare_product_basis(
        self,
        request: ProductArtifactBasisRequest,
    ) -> ProductArtifactBasisResult:
        if type(request) is not ProductArtifactBasisRequest:
            raise TypeError(
                "ArtifactRenderingTool requires ProductArtifactBasisRequest"
            )
        claim_refs = [claim.claim_id for claim in request.evidence_packet.claims]
        evidence_refs = list(
            dict.fromkeys(
                ref
                for claim in request.evidence_packet.claims
                for ref in claim.evidence_refs
            )
        )
        material = {
            "episode_id": request.episode_id,
            "audience_role": request.audience_role,
            "accepted_evidence_ref": request.accepted_evidence_ref,
            "accepted_evidence_hash": request.accepted_evidence_hash,
            "claim_refs": claim_refs,
            "evidence_refs": evidence_refs,
        }
        return ProductArtifactBasisResult(
            artifact_basis_id=f"artifact-basis:{stable_hash(material)[:24]}",
            **material,
            source_refs=[request.accepted_evidence_ref, *evidence_refs],
        )


__all__ = [
    "ARTIFACT_RENDERING_TOOL_VERSION",
    "ArtifactRenderRequest",
    "ArtifactRenderResult",
    "ArtifactRenderingTool",
    "AudienceRole",
    "ROLE_REPORT_SAFETY_NOTICES",
    "ProductArtifactBasisRequest",
    "ProductArtifactBasisResult",
]
