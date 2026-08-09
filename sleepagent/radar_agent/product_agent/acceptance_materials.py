from __future__ import annotations

import argparse
import hashlib
import json
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

from pydantic import Field

from sleepagent.radar_agent.product_agent.acceptance import (
    AcceptanceEvidenceKind,
    AcceptanceManifest,
    AcceptanceObservation,
    AcceptanceScenario,
    HabitConceptDomainReview,
    HabitDomainReviewCatalog,
    HabitDomainReviewFinding,
    HabitDomainReviewReport,
    HabitDomainReviewer,
    HabitDomainReviewScope,
    HabitDomainReviewSignoff,
    HabitUsabilityAttestation,
    HabitUsabilityObservation,
    HabitUsabilityReport,
    ProviderRunReceipt,
    ProductAgentReleaseVerifier,
    current_habit_catalog_hash,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    EvidenceSemantic,
    StrictContract,
    stable_hash,
)
from sleepagent.radar_agent.questionnaire import DEFAULT_HABIT_CONCEPTS


_TEMPLATE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
_SIMULATED_TIME = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
_MATERIAL_FILENAMES = (
    "habit_usability_report.json",
    "habit_domain_review.json",
    "provider_observations.json",
    "README.md",
)
_ARCHIVE_MAX_FILES = 20
_ARCHIVE_MAX_MEMBER_BYTES = 5 * 1024 * 1024
_ARCHIVE_MAX_TOTAL_BYTES = 10 * 1024 * 1024


class AcceptanceMaterialFinding(StrictContract):
    code: str
    severity: Literal["info", "warning", "error"]
    material: str
    message: str


class AcceptanceMaterialAudit(StrictContract):
    schema_version: Literal["sleepagent-acceptance-material-audit.v1"] = (
        "sleepagent-acceptance-material-audit.v1"
    )
    material_root: str
    usability_observations: int = Field(default=0, ge=0)
    domain_concepts: int = Field(default=0, ge=0)
    provider_observations: int = Field(default=0, ge=0)
    usable_as_simulation_fixture: bool
    release_evidence_eligible: bool
    findings: tuple[AcceptanceMaterialFinding, ...]


class AcceptanceMaterialTemplateResult(StrictContract):
    schema_version: Literal["sleepagent-acceptance-material-template.v1"] = (
        "sleepagent-acceptance-material-template.v1"
    )
    material_root: str
    release_identity_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    habit_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    habit_concepts: int = Field(..., ge=1)
    provider_observation_slots: int = Field(..., ge=1)
    files: tuple[str, ...]
    release_evidence_eligible: Literal[False] = False


class AcceptanceMaterialSimulationResult(StrictContract):
    schema_version: Literal[
        "sleepagent-acceptance-material-simulation.v1"
    ] = "sleepagent-acceptance-material-simulation.v1"
    material_root: str
    release_identity_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    habit_catalog_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    usability_observations: int = Field(..., ge=3, le=5)
    habit_concepts: int = Field(..., ge=1)
    provider_observations: int = Field(..., ge=1)
    files: tuple[str, ...]
    release_evidence_eligible: Literal[False] = False


class AcceptanceMaterialArchiveResult(StrictContract):
    schema_version: Literal[
        "sleepagent-acceptance-material-archive.v1"
    ] = "sleepagent-acceptance-material-archive.v1"
    archive_path: str
    archive_root: str
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    files: tuple[str, ...]


def initialize_acceptance_materials(
    root: Path,
    *,
    manifest: AcceptanceManifest,
) -> AcceptanceMaterialTemplateResult:
    """Create current, deliberately ineligible acceptance collection templates.

    The generated rows are bound to the supplied release identity and current
    Habit catalog, but every placeholder contains an irreversible ``template``
    provenance marker. Real evidence must be recorded from actual sessions and
    executions rather than promoted by flipping ``evidence_kind``.
    """

    targets = tuple(root / filename for filename in _MATERIAL_FILENAMES)
    existing = tuple(path.name for path in targets if path.exists())
    if existing:
        raise FileExistsError(
            "refusing to overwrite acceptance materials: "
            + ", ".join(existing)
        )

    usability = _usability_template()
    domain = _domain_review_template()
    observations = _provider_observation_templates(manifest)
    payloads = {
        "habit_usability_report.json": usability.model_dump_json(indent=2),
        "habit_domain_review.json": domain.model_dump_json(indent=2),
        "provider_observations.json": json.dumps(
            {
                "template_notice": (
                    "Collection slots only. Replace each row with evidence from "
                    "the corresponding actually executed scenario."
                ),
                "observations": [
                    item.model_dump(mode="json") for item in observations
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        "README.md": _template_readme(manifest),
    }

    root.mkdir(parents=True, exist_ok=True)
    for filename, content in payloads.items():
        with (root / filename).open("x", encoding="utf-8") as stream:
            stream.write(content.rstrip() + "\n")

    return AcceptanceMaterialTemplateResult(
        material_root=str(root),
        release_identity_hash=manifest.release_identity.identity_hash,
        habit_catalog_hash=current_habit_catalog_hash(),
        habit_concepts=len(DEFAULT_HABIT_CONCEPTS),
        provider_observation_slots=len(observations),
        files=_MATERIAL_FILENAMES,
    )


def initialize_simulated_acceptance_materials(
    root: Path,
    *,
    manifest: AcceptanceManifest,
) -> AcceptanceMaterialSimulationResult:
    """Create complete, current and explicitly synthetic development evidence."""

    targets = tuple(root / filename for filename in _MATERIAL_FILENAMES)
    existing = tuple(path.name for path in targets if path.exists())
    if existing:
        raise FileExistsError(
            "refusing to overwrite acceptance materials: "
            + ", ".join(existing)
        )

    usability = _simulated_usability_report()
    domain = _simulated_domain_review()
    observations = _simulated_provider_observations(manifest)
    payloads = {
        "habit_usability_report.json": usability.model_dump_json(indent=2),
        "habit_domain_review.json": domain.model_dump_json(indent=2),
        "provider_observations.json": json.dumps(
            {
                "simulation_notice": (
                    "Synthetic development observations only; no provider "
                    "requests in this file were actually executed."
                ),
                "observations": [
                    item.model_dump(mode="json") for item in observations
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        "README.md": _simulated_readme(manifest),
    }
    root.mkdir(parents=True, exist_ok=True)
    for filename, content in payloads.items():
        with (root / filename).open("x", encoding="utf-8") as stream:
            stream.write(content.rstrip() + "\n")

    return AcceptanceMaterialSimulationResult(
        material_root=str(root),
        release_identity_hash=manifest.release_identity.identity_hash,
        habit_catalog_hash=current_habit_catalog_hash(),
        usability_observations=len(usability.observations),
        habit_concepts=len(DEFAULT_HABIT_CONCEPTS),
        provider_observations=len(observations),
        files=_MATERIAL_FILENAMES,
    )


def build_acceptance_material_archive(
    root: Path,
    archive: Path,
    *,
    archive_root: str,
) -> AcceptanceMaterialArchiveResult:
    """Build a byte-stable ZIP from one canonical material directory.

    Archive member order, timestamps, permissions and compression are fixed so
    checked-in fixture hashes do not depend on filesystem metadata or zlib.
    Existing archives are never overwritten.
    """

    archive_path = PurePosixPath(archive_root)
    if (
        not archive_root
        or archive_path.is_absolute()
        or len(archive_path.parts) != 1
        or archive_path.parts[0] in {".", ".."}
        or "\\" in archive_root
    ):
        raise ValueError("archive_root must be one safe directory name")
    missing = tuple(
        filename
        for filename in _MATERIAL_FILENAMES
        if not (root / filename).is_file()
    )
    if missing:
        raise FileNotFoundError(
            "acceptance material archive is missing: " + ", ".join(missing)
        )
    if archive.exists():
        raise FileExistsError(f"refusing to overwrite archive: {archive}")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, mode="x", compression=zipfile.ZIP_STORED) as bundle:
        for filename in _MATERIAL_FILENAMES:
            member = zipfile.ZipInfo(
                filename=f"{archive_root}/{filename}",
                date_time=(2000, 1, 1, 0, 0, 0),
            )
            member.create_system = 3
            member.compress_type = zipfile.ZIP_STORED
            member.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(member, (root / filename).read_bytes())

    return AcceptanceMaterialArchiveResult(
        archive_path=str(archive),
        archive_root=archive_root,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        files=_MATERIAL_FILENAMES,
    )


def _simulated_usability_report() -> HabitUsabilityReport:
    participant_specs = (
        ("participant:simulated-60-01", "60-69", 2, 2),
        ("participant:simulated-60-02", "60-69", 3, 2),
        ("participant:simulated-70-01", "70-79", 2, 1),
        ("participant:simulated-70-02", "70-79", 3, 3),
        ("participant:simulated-80-01", "80+", 2, 2),
    )
    observations = [
        HabitUsabilityObservation(
            participant_ref=participant_ref,
            age_band=age_band,
            understood_personalization_purpose=True,
            understood_questions_are_skippable=True,
            understood_separate_persistence_confirmation=True,
            skip_attempt_succeeded=True,
            questions_presented=questions,
            erroneous_confirmation=False,
            interruption_rating=interruption,
            notes=(
                "Synthetic usability interaction for pipeline regression; "
                "no real participant was observed."
            ),
        )
        for participant_ref, age_band, questions, interruption in participant_specs
    ]
    refs = tuple(item.participant_ref for item in observations)
    return HabitUsabilityReport(
        evidence_kind=AcceptanceEvidenceKind.SIMULATED,
        report_id="habit-usability:simulated-v24-current",
        conducted_at=_SIMULATED_TIME,
        observations=observations,
        reviewer_ref="facilitator:simulated-usability-v24-current",
        attestation=HabitUsabilityAttestation(
            protocol_version="sleep-habit-usability.v1",
            facilitator_ref="facilitator:simulated-usability-v24-current",
            observed_participant_refs=refs,
            participant_interactions_observed=True,
            synthetic_data_used=True,
            signed_at=_SIMULATED_TIME,
            signature_reference=(
                "signature:simulated-usability-v24-current-not-real"
            ),
        ),
    )


def _simulated_domain_review() -> HabitDomainReviewReport:
    reviewers = (
        HabitDomainReviewer(
            reviewer_ref="reviewer:simulated-sleep-medicine-v24-current",
            display_name="模拟睡眠医学审核员 A（非真人）",
            professional_role="simulated sleep-medicine reviewer",
            qualification="synthetic qualification; not a real license",
            organization_ref="organization:simulated-sleep-center",
            conflict_of_interest="synthetic fixture; no real declaration",
        ),
        HabitDomainReviewer(
            reviewer_ref="reviewer:simulated-geriatric-ux-v24-current",
            display_name="模拟老年可用性审核员 B（非真人）",
            professional_role="simulated geriatric usability reviewer",
            qualification="synthetic qualification; not a real credential",
            organization_ref="organization:simulated-geriatric-lab",
            conflict_of_interest="synthetic fixture; no real declaration",
        ),
    )
    reviewer_refs = tuple(item.reviewer_ref for item in reviewers)

    def finding(
        dimension: str,
        *,
        eligible: bool | None = None,
    ) -> HabitDomainReviewFinding:
        return HabitDomainReviewFinding(
            decision="approved",
            reviewer_refs=reviewer_refs,
            finding=(
                f"Simulated {dimension} approval for schema and audit "
                "pipeline regression only."
            ),
            eligible=eligible,
        )

    concept_reviews = tuple(
        HabitConceptDomainReview(
            concept_id=concept.concept_id,
            version=concept.version,
            content_version=concept.content_version,
            valid_for_days=concept.valid_for_days,
            domain_review_status="approved",
            reviewer_ref=reviewers[0].reviewer_ref,
            wording_review=finding("wording"),
            options_review=finding("answer options"),
            ttl_review=finding("TTL"),
            persistence_eligibility_review=finding(
                "persistence eligibility",
                eligible=concept.persistence.value == "profile_eligible",
            ),
            safety_review=finding("safety boundary"),
            final_decision="approved",
            reviewed_at=_SIMULATED_TIME,
            approval_record_ref=(
                f"approval:simulated-v24-current:{concept.concept_id}"
            ),
        )
        for concept in DEFAULT_HABIT_CONCEPTS
    )
    content_versions = {
        concept.content_version for concept in DEFAULT_HABIT_CONCEPTS
    }
    if len(content_versions) != 1:
        raise ValueError(
            "current Habit catalog has multiple content versions; "
            "a single simulated review cannot bind it"
        )
    return HabitDomainReviewReport(
        evidence_kind=AcceptanceEvidenceKind.SIMULATED,
        review_report_id="habit-domain-review:simulated-v24-current",
        review_type="catalog_wording_options_ttl_persistence",
        reviewed_at=_SIMULATED_TIME,
        catalog=HabitDomainReviewCatalog(
            catalog_id="habit-question-catalog",
            catalog_version="1.0.0",
            catalog_hash=current_habit_catalog_hash(),
            content_version=next(iter(content_versions)),
            concept_count=len(DEFAULT_HABIT_CONCEPTS),
        ),
        reviewers=reviewers,
        scope=HabitDomainReviewScope(
            wording=True,
            options=True,
            ttl=True,
            persistence_eligibility=True,
            source_semantics=True,
            safety_escalation_boundary=True,
        ),
        concept_reviews=concept_reviews,
        overall_decision="approved",
        overall_findings=(
            "Synthetic approval exercises the complete review schema only.",
        ),
        approval_reference="approval:simulated-v24-current-bundle-not-real",
        signoff=HabitDomainReviewSignoff(
            status="approved",
            signed_at=_SIMULATED_TIME,
            signed_by=reviewer_refs,
            signature_reference="signature:simulated-v24-current-not-real",
        ),
    )


def _simulated_provider_observations(
    manifest: AcceptanceManifest,
) -> tuple[AcceptanceObservation, ...]:
    deterministic = {
        AcceptanceScenario.DATA_QUALITY,
        AcceptanceScenario.URGENT,
    }
    safety = {
        AcceptanceScenario.URGENT,
        AcceptanceScenario.SAFETY_REVISION,
        AcceptanceScenario.HABIT_SAFETY_PREEMPTION,
    }
    care = {
        AcceptanceScenario.CARE_PLAN,
        AcceptanceScenario.CARE_FOLLOWUP,
        AcceptanceScenario.EXTERNAL_ACTION,
        AcceptanceScenario.DOCTOR_MATERIAL,
    }
    observations: list[AcceptanceObservation] = []
    for scenario in AcceptanceScenario:
        repetitions = (1,) if scenario in deterministic else (1, 2, 3)
        for repetition in repetitions:
            agents = [AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING]
            if scenario in care:
                agents.append(AgentId.CARE_STRATEGY)
            if scenario in safety:
                agents.append(AgentId.SAFETY_REVIEW)
            receipt = None
            if scenario not in deterministic:
                material = {
                    "kind": "synthetic-provider-receipt",
                    "scenario": scenario.value,
                    "repetition": repetition,
                    "release_identity_hash": (
                        manifest.release_identity.identity_hash
                    ),
                }
                receipt = ProviderRunReceipt(
                    receipt_ref=(
                        f"trace:simulated-v24-current:{scenario.value}:"
                        f"{repetition}"
                    ),
                    receipt_hash=stable_hash(material),
                    providers=("simulated-openai-compatible-provider",),
                    model_ids=("simulated-model-v24-current",),
                    provider_request_ids=(
                        "request:simulated-v24-current:"
                        f"{scenario.value}:{repetition}",
                    ),
                    invocation_ids=(
                        "invocation:simulated-v24-current:"
                        f"{scenario.value}:{repetition}",
                    ),
                    executed_at=_SIMULATED_TIME,
                )
            observations.append(
                AcceptanceObservation(
                    observation_id=(
                        f"observation:simulated-v24-current:{scenario.value}:"
                        f"{repetition}"
                    ),
                    scenario=scenario,
                    release_identity_hash=(
                        manifest.release_identity.identity_hash
                    ),
                    repetition=repetition,
                    role="elder",
                    observed_agents=agents,
                    evidence_semantics=[EvidenceSemantic.OBSERVED_FACT],
                    evidence_kind=AcceptanceEvidenceKind.SIMULATED,
                    real_provider=False,
                    provider_receipt=receipt,
                    domain_reviewed=False,
                    passed=True,
                )
            )
    return tuple(observations)


def _simulated_readme(manifest: AcceptanceManifest) -> str:
    return f"""# Complete simulated acceptance fixture

This directory is synthetic development data, not release evidence.

- release identity: `{manifest.release_identity.identity_hash}`
- Habit catalog: `{current_habit_catalog_hash()}`
- usability interactions: simulated, with synthetic attestation
- reviewers and signatures: simulated identities and synthetic credentials
- provider receipts/request IDs: deterministic synthetic shapes; no API call ran

The fixture exercises every schema and all 68 scenario/repetition slots. It
must remain `evidence_kind=simulated` and `real_provider=false`. Use
`sleep_habit_profile/real-evidence-v23-collection` for actual collection.

Audit with:

```bash
python -m sleepagent.radar_agent.product_agent.acceptance_materials \\
  <this-material-directory> \\
  --manifest agent_architecture/ACCEPTANCE-MANIFEST.json
```
"""


def _usability_template() -> HabitUsabilityReport:
    return HabitUsabilityReport(
        evidence_kind=AcceptanceEvidenceKind.SIMULATED,
        report_id="habit-usability:template-replace",
        conducted_at=_TEMPLATE_TIME,
        observations=[
            HabitUsabilityObservation(
                participant_ref=f"participant:template-replace-{index}",
                age_band="60-69",
                understood_personalization_purpose=False,
                understood_questions_are_skippable=False,
                understood_separate_persistence_confirmation=False,
                skip_attempt_succeeded=False,
                questions_presented=0,
                erroneous_confirmation=True,
                interruption_rating=5,
                notes="Template slot; replace with an actually observed interaction.",
            )
            for index in range(1, 4)
        ],
        reviewer_ref="reviewer:template-replace",
    )


def _domain_review_template() -> HabitDomainReviewReport:
    reviewer = HabitDomainReviewer(
        reviewer_ref="reviewer:template-replace",
        display_name="Template reviewer — replace",
        professional_role="Template role — replace",
        qualification="Template qualification — replace",
        organization_ref="organization:template-replace",
        conflict_of_interest="Template declaration — replace",
    )

    def finding(
        dimension: str,
        *,
        eligible: bool | None = None,
    ) -> HabitDomainReviewFinding:
        return HabitDomainReviewFinding(
            decision="changes_requested",
            reviewer_refs=(reviewer.reviewer_ref,),
            finding=f"Template {dimension} finding; replace with signed review.",
            required_changes=(f"Complete the {dimension} review.",),
            eligible=eligible,
        )

    concept_reviews = tuple(
        HabitConceptDomainReview(
            concept_id=concept.concept_id,
            version=concept.version,
            content_version=concept.content_version,
            valid_for_days=concept.valid_for_days,
            domain_review_status="rejected",
            reviewer_ref=reviewer.reviewer_ref,
            wording_review=finding("wording"),
            options_review=finding("answer options"),
            ttl_review=finding("TTL"),
            persistence_eligibility_review=finding(
                "persistence eligibility",
                eligible=concept.persistence.value == "profile_eligible",
            ),
            safety_review=finding("safety boundary"),
            final_decision="changes_requested",
            reviewed_at=_TEMPLATE_TIME,
            approval_record_ref=(
                f"approval:template-replace:{concept.concept_id}"
            ),
        )
        for concept in DEFAULT_HABIT_CONCEPTS
    )
    content_versions = {
        concept.content_version for concept in DEFAULT_HABIT_CONCEPTS
    }
    if len(content_versions) != 1:
        raise ValueError(
            "current Habit catalog has multiple content versions; "
            "a single review template cannot bind it"
        )
    return HabitDomainReviewReport(
        evidence_kind=AcceptanceEvidenceKind.SIMULATED,
        review_report_id="habit-domain-review:template-replace",
        review_type="catalog_wording_options_ttl_persistence",
        reviewed_at=_TEMPLATE_TIME,
        catalog=HabitDomainReviewCatalog(
            catalog_id="habit-question-catalog",
            catalog_version="1.0.0",
            catalog_hash=current_habit_catalog_hash(),
            content_version=next(iter(content_versions)),
            concept_count=len(DEFAULT_HABIT_CONCEPTS),
        ),
        reviewers=(reviewer,),
        scope=HabitDomainReviewScope(
            wording=True,
            options=True,
            ttl=True,
            persistence_eligibility=True,
            source_semantics=True,
            safety_escalation_boundary=True,
        ),
        concept_reviews=concept_reviews,
        overall_decision="changes_requested",
        overall_findings=(
            "Template only; every concept requires an actual named review.",
        ),
        changes_requested=("Replace every template review and signoff.",),
        approval_reference="approval:template-replace",
        signoff=HabitDomainReviewSignoff(
            status="changes_requested",
            signed_at=_TEMPLATE_TIME,
            signed_by=(reviewer.reviewer_ref,),
            signature_reference="signature:template-replace",
        ),
    )


def _provider_observation_templates(
    manifest: AcceptanceManifest,
) -> tuple[AcceptanceObservation, ...]:
    deterministic = {
        AcceptanceScenario.DATA_QUALITY,
        AcceptanceScenario.URGENT,
    }
    return tuple(
        AcceptanceObservation(
            observation_id=(
                f"template:{scenario.value}:replace-run:{repetition}"
            ),
            scenario=scenario,
            release_identity_hash=manifest.release_identity.identity_hash,
            repetition=repetition,
            role="elder",
            observed_agents=[],
            evidence_kind=AcceptanceEvidenceKind.SIMULATED,
            real_provider=False,
            provider_receipt=None,
            domain_reviewed=False,
            passed=False,
        )
        for scenario in AcceptanceScenario
        for repetition in ((1,) if scenario in deterministic else (1, 2, 3))
    )


def _template_readme(manifest: AcceptanceManifest) -> str:
    return f"""# Acceptance evidence collection templates

These files are collection slots, not release evidence. They are bound to:

- release identity: `{manifest.release_identity.identity_hash}`
- Habit catalog: `{current_habit_catalog_hash()}`

Do not promote a placeholder by changing `evidence_kind`. Replace it with data
from an actually observed 60+ participant session, an actual named professional
review/signature, or the corresponding actually executed provider scenario.
Placeholder identifiers deliberately contain `template`, so the production
schemas reject them as real evidence.

After collection, run:

```bash
python -m sleepagent.radar_agent.product_agent.acceptance_materials \\
  <this-material-directory> \\
  --manifest agent_architecture/ACCEPTANCE-MANIFEST.json
```

See `sleep_habit_profile/ACCEPTANCE-EVIDENCE-FORMAT.md` and
`sleep_habit_profile/USABILITY-TEST-PROTOCOL.md` for the field definitions and
study procedure.
"""


def audit_acceptance_materials(
    root: Path,
    *,
    manifest: AcceptanceManifest,
) -> AcceptanceMaterialAudit:
    findings: list[AcceptanceMaterialFinding] = []
    usability_count = 0
    domain_count = 0
    provider_count = 0
    usability = None
    domain = None

    usability_path = _resolve_material(root, "habit_usability_report")
    domain_path = _resolve_material(root, "habit_domain_review")
    provider_path = _resolve_material(root, "provider_observations")
    required = (usability_path, domain_path, provider_path)
    missing = [item for item in required if not item.is_file()]
    for item in missing:
        findings.append(
            AcceptanceMaterialFinding(
                code="material.missing",
                severity="error",
                material=item.name,
                message="required acceptance material is missing",
            )
        )
    if missing:
        return _audit(
            root,
            usability_count,
            domain_count,
            provider_count,
            findings,
        )

    usability = _load_model(usability_path, HabitUsabilityReport, findings)
    if usability is not None:
        usability_count = len(usability.observations)
        if usability.evidence_kind != AcceptanceEvidenceKind.REAL:
            findings.append(
                AcceptanceMaterialFinding(
                    code="usability.simulated",
                    severity="warning",
                    material=usability_path.name,
                    message=(
                        "simulated participants may test the pipeline but cannot "
                        "satisfy the real 3-5 participant release gate"
                    ),
                )
            )

    domain = _load_model(domain_path, HabitDomainReviewReport, findings)
    if domain is not None:
        domain_count = len(domain.concept_reviews)
        if domain.evidence_kind != AcceptanceEvidenceKind.REAL:
            findings.append(
                AcceptanceMaterialFinding(
                    code="domain.simulated",
                    severity="warning",
                    material=domain_path.name,
                    message="simulated reviewers cannot approve the production catalog",
                )
            )
        for error in domain.current_catalog_errors():
            findings.append(
                AcceptanceMaterialFinding(
                    code="domain.catalog_drift",
                    severity="error",
                    material=domain_path.name,
                    message=error,
                )
            )

    provider_raw = _load_json(provider_path, findings)
    rows = (
        provider_raw.get("observations", [])
        if isinstance(provider_raw, dict)
        else []
    )
    if not isinstance(rows, list):
        findings.append(
            AcceptanceMaterialFinding(
                code="provider.invalid_observations",
                severity="error",
                material=provider_path.name,
                message="observations must be an array",
            )
        )
        rows = []
    provider_count = len(rows)
    known = {item.value for item in AcceptanceScenario}
    supplied = {
        str(item.get("scenario"))
        for item in rows
        if isinstance(item, dict) and item.get("scenario")
    }
    unknown = sorted(supplied - known)
    omitted = sorted(known - supplied)
    if unknown or omitted:
        findings.append(
            AcceptanceMaterialFinding(
                code="provider.scenario_drift",
                severity="error",
                material=provider_path.name,
                message=f"unknown={unknown}; omitted={omitted}",
            )
        )
    release_hashes = {
        str(item.get("release_identity_hash"))
        for item in rows
        if isinstance(item, dict) and item.get("release_identity_hash")
    }
    if release_hashes != {manifest.release_identity.identity_hash}:
        findings.append(
            AcceptanceMaterialFinding(
                code="provider.release_identity_mismatch",
                severity="error",
                material=provider_path.name,
                message=(
                    "provider observations are not bound to the current "
                    f"release identity {manifest.release_identity.identity_hash}"
                ),
            )
        )
    invalid_rows = 0
    validated_rows: list[AcceptanceObservation] = []
    for row in rows:
        try:
            validated_rows.append(AcceptanceObservation.model_validate(row))
        except Exception:
            invalid_rows += 1
    if invalid_rows:
        findings.append(
            AcceptanceMaterialFinding(
                code="provider.unverifiable_receipts",
                severity="error",
                material=provider_path.name,
                message=(
                    f"{invalid_rows} observations lack a valid provenance/receipt "
                    "binding or use an unknown scenario"
                ),
            )
        )
    elif usability is not None and domain is not None:
        try:
            candidate = AcceptanceManifest(
                manifest_version=manifest.manifest_version,
                release_version=manifest.release_version,
                release_identity=manifest.release_identity,
                observations=validated_rows,
                habit_domain_review_report=domain,
                habit_usability_report=usability,
            )
            gate = ProductAgentReleaseVerifier().verify(candidate)
            if not gate.eligible:
                findings.append(
                    AcceptanceMaterialFinding(
                        code="release.gate_failed",
                        severity="error",
                        material=str(root),
                        message=gate.model_dump_json(),
                    )
                )
        except Exception as exc:
            findings.append(
                AcceptanceMaterialFinding(
                    code="release.manifest_invalid",
                    severity="error",
                    material=str(root),
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    return _audit(
        root,
        usability_count,
        domain_count,
        provider_count,
        findings,
    )


def audit_acceptance_material_archive(
    archive: Path,
    *,
    manifest: AcceptanceManifest,
) -> AcceptanceMaterialAudit:
    """Audit the three canonical materials in a bounded ZIP without extracting it."""

    try:
        with zipfile.ZipFile(archive) as bundle:
            members = tuple(item for item in bundle.infolist() if not item.is_dir())
            _validate_archive_members(members)
            selected = {
                stem: _select_archive_material(members, stem)
                for stem in (
                    "habit_usability_report",
                    "habit_domain_review",
                    "provider_observations",
                )
            }
            parents = {
                str(PurePosixPath(item.filename).parent)
                for item in selected.values()
            }
            if len(parents) != 1:
                raise ValueError(
                    "canonical acceptance materials must share one archive directory"
                )
            with tempfile.TemporaryDirectory(
                prefix="sleepagent-acceptance-audit-"
            ) as temporary:
                root = Path(temporary)
                for stem, member in selected.items():
                    payload = bundle.read(member)
                    if len(payload) > _ARCHIVE_MAX_MEMBER_BYTES:
                        raise ValueError(
                            f"archive member exceeds size limit: {member.filename}"
                        )
                    (root / f"{stem}.json").write_bytes(payload)
                report = audit_acceptance_materials(root, manifest=manifest)
                findings = tuple(
                    item.model_copy(update={"material": str(archive)})
                    if item.material == str(root)
                    else item
                    for item in report.findings
                )
                return report.model_copy(
                    update={
                        "material_root": str(archive),
                        "findings": findings,
                    }
                )
    except Exception as exc:
        return _audit(
            archive,
            0,
            0,
            0,
            [
                AcceptanceMaterialFinding(
                    code="archive.invalid",
                    severity="error",
                    material=str(archive),
                    message=f"{type(exc).__name__}: {exc}",
                )
            ],
        )


def _validate_archive_members(
    members: tuple[zipfile.ZipInfo, ...],
) -> None:
    if not members:
        raise ValueError("acceptance archive is empty")
    if len(members) > _ARCHIVE_MAX_FILES:
        raise ValueError("acceptance archive contains too many files")
    total_size = sum(item.file_size for item in members)
    if total_size > _ARCHIVE_MAX_TOTAL_BYTES:
        raise ValueError("acceptance archive exceeds total size limit")
    for member in members:
        path = PurePosixPath(member.filename)
        unix_mode = member.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in member.filename
        ):
            raise ValueError(f"unsafe archive path: {member.filename}")
        if member.flag_bits & 0x1:
            raise ValueError(f"encrypted archive member: {member.filename}")
        if stat.S_ISLNK(unix_mode):
            raise ValueError(f"archive symlink is not allowed: {member.filename}")
        if member.file_size > _ARCHIVE_MAX_MEMBER_BYTES:
            raise ValueError(
                f"archive member exceeds size limit: {member.filename}"
            )


def _select_archive_material(
    members: tuple[zipfile.ZipInfo, ...],
    stem: str,
) -> zipfile.ZipInfo:
    names = {f"{stem}.json", f"{stem}.simulated.json"}
    matches = tuple(
        item
        for item in members
        if PurePosixPath(item.filename).name in names
    )
    if len(matches) != 1:
        raise ValueError(
            f"archive must contain exactly one {stem} material; "
            f"found {len(matches)}"
        )
    return matches[0]


def _load_model(path: Path, model, findings):
    raw = _load_json(path, findings)
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except Exception as exc:
        findings.append(
            AcceptanceMaterialFinding(
                code="material.schema_invalid",
                severity="error",
                material=path.name,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return None


def _resolve_material(root: Path, stem: str) -> Path:
    canonical = root / f"{stem}.json"
    return canonical if canonical.is_file() else root / f"{stem}.simulated.json"


def _load_json(path: Path, findings):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        findings.append(
            AcceptanceMaterialFinding(
                code="material.json_invalid",
                severity="error",
                material=path.name,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return None


def _audit(
    root: Path,
    usability_count: int,
    domain_count: int,
    provider_count: int,
    findings: list[AcceptanceMaterialFinding],
) -> AcceptanceMaterialAudit:
    errors = any(item.severity == "error" for item in findings)
    simulated = any(item.code.endswith(".simulated") for item in findings)
    return AcceptanceMaterialAudit(
        material_root=str(root),
        usability_observations=usability_count,
        domain_concepts=domain_count,
        provider_observations=provider_count,
        usable_as_simulation_fixture=not any(
            item.code
            in {
                "archive.invalid",
                "material.missing",
                "material.json_invalid",
                "material.schema_invalid",
            }
            for item in findings
        ),
        release_evidence_eligible=not errors and not simulated,
        findings=tuple(findings),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acceptance-material-audit")
    parser.add_argument("material_root", type=Path)
    parser.add_argument(
        "--initialize-templates",
        action="store_true",
        help=(
            "create current, deliberately ineligible collection templates; "
            "existing material files are never overwritten"
        ),
    )
    parser.add_argument(
        "--initialize-simulated-fixture",
        action="store_true",
        help=(
            "create complete current synthetic development evidence; "
            "never release eligible and never overwrites existing files"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("agent_architecture/ACCEPTANCE-MANIFEST.json"),
    )
    args = parser.parse_args(argv)
    manifest = AcceptanceManifest.model_validate(
        json.loads(args.manifest.read_text(encoding="utf-8"))
    )
    if args.initialize_templates and args.initialize_simulated_fixture:
        parser.error(
            "choose only one initializer: templates or simulated fixture"
        )
    if args.initialize_templates or args.initialize_simulated_fixture:
        if args.material_root.suffix.lower() == ".zip":
            parser.error("material initialization requires a directory, not a ZIP")
        result = (
            initialize_acceptance_materials(
                args.material_root,
                manifest=manifest,
            )
            if args.initialize_templates
            else initialize_simulated_acceptance_materials(
                args.material_root,
                manifest=manifest,
            )
        )
        print(result.model_dump_json(indent=2))
        return 0
    report = (
        audit_acceptance_material_archive(
            args.material_root,
            manifest=manifest,
        )
        if args.material_root.suffix.lower() == ".zip"
        else audit_acceptance_materials(
            args.material_root,
            manifest=manifest,
        )
    )
    print(report.model_dump_json(indent=2))
    return 0 if report.release_evidence_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AcceptanceMaterialArchiveResult",
    "AcceptanceMaterialAudit",
    "AcceptanceMaterialFinding",
    "AcceptanceMaterialSimulationResult",
    "AcceptanceMaterialTemplateResult",
    "audit_acceptance_material_archive",
    "audit_acceptance_materials",
    "build_acceptance_material_archive",
    "initialize_acceptance_materials",
    "initialize_simulated_acceptance_materials",
    "main",
]
