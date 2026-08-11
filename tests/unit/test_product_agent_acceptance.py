from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from sleepagent.product_runtime.acceptance import (
    ACCEPTANCE_MANIFEST_VERSION,
    AcceptanceEvidenceKind,
    AcceptanceManifest,
    AcceptanceObservation,
    AcceptanceScenario,
    ExternalActionAcceptanceReceipt,
    HardViolation,
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
    ReleaseGateError,
    RuntimeRunReceipt,
    StatePersistenceAcceptanceReceipt,
    current_acceptance_release_identity,
    current_habit_catalog_hash,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    EpisodeStatus,
    ExecutionMode,
    stable_hash,
)
from sleepagent.product_runtime.external_actions import (
    PRODUCT_EXTERNAL_ACTION_VERSION,
)
from sleepagent.product_runtime.product_persistence import (
    PRODUCT_STATE_PERSISTENCE_VERSION,
)
from sleepagent.product_runtime.registry import product_agent_manifest
from sleepagent.product_runtime.runtime_factory import (
    PRODUCT_EPISODE_API_ADAPTER_VERSION,
)
from sleepagent.product_runtime.questionnaire import DEFAULT_HABIT_CONCEPTS
from sleepagent.product_runtime.acceptance_materials import (
    audit_acceptance_material_archive,
    audit_acceptance_materials,
    build_acceptance_material_archive,
    initialize_acceptance_materials,
    initialize_simulated_acceptance_materials,
)


REPOSITORY = Path(__file__).parents[2]


def _build_fixture_archive(
    tmp_path: Path,
    *,
    material_root: Path,
    archive_root: str,
    expected_sha256: str,
) -> Path:
    archive = tmp_path / f"{archive_root}.zip"
    result = build_acceptance_material_archive(
        material_root,
        archive,
        archive_root=archive_root,
    )

    assert result.archive_path == str(archive)
    assert result.archive_root == archive_root
    assert result.sha256 == expected_sha256
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_sha256
    return archive


def empty_manifest() -> AcceptanceManifest:
    return AcceptanceManifest(
        release_identity=current_acceptance_release_identity()
    )


def provider_receipt(scenario: AcceptanceScenario, repetition: int) -> ProviderRunReceipt:
    material = {"scenario": scenario.value, "repetition": repetition}
    return ProviderRunReceipt(
        receipt_ref=f"receipt:{scenario.value}:{repetition}",
        receipt_hash=stable_hash(material),
        providers=("test-real-provider",),
        model_ids=("test-model",),
        provider_request_ids=(f"provider-request:{scenario.value}:{repetition}",),
        invocation_ids=(f"invocation:{scenario.value}:{repetition}",),
        executed_at=datetime(2026, 7, 26, repetition, tzinfo=timezone.utc),
    )


def runtime_receipt(
    scenario: AcceptanceScenario,
    repetition: int,
) -> RuntimeRunReceipt:
    deterministic = scenario in {
        AcceptanceScenario.DATA_QUALITY,
        AcceptanceScenario.URGENT,
    }
    return RuntimeRunReceipt(
        episode_id=f"episode:{scenario.value}:{repetition}",
        trace_ref=f"trace:{scenario.value}:{repetition}",
        result_hash=stable_hash(
            {"runtime": scenario.value, "repetition": repetition}
        ),
        status=EpisodeStatus.COMPLETE,
        execution_mode=(
            ExecutionMode.DETERMINISTIC_ONLY
            if deterministic
            else ExecutionMode.INTELLIGENT
        ),
        recorded_at=datetime(
            2026,
            7,
            26,
            repetition,
            tzinfo=timezone.utc,
        ),
    )


def external_action_receipt(
    repetition: int,
) -> ExternalActionAcceptanceReceipt:
    return ExternalActionAcceptanceReceipt(
        tool_receipt_id=f"external-receipt:{repetition}",
        tool_receipt_hash=stable_hash({"external-receipt": repetition}),
        tool_name="external.share",
        target_hash=stable_hash({"external-target": repetition}),
        provider="real-external-gateway",
        provider_request_id=f"external-request:{repetition}",
        delivery_status="delivered",
        executed_at=datetime(
            2026,
            7,
            26,
            repetition,
            tzinfo=timezone.utc,
        ),
    )


def state_persistence_receipts(
    scenario: AcceptanceScenario,
    repetition: int,
) -> tuple[StatePersistenceAcceptanceReceipt, ...]:
    state_kind = {
        AcceptanceScenario.MEMORY: "memory",
        AcceptanceScenario.CARE_PLAN: "care",
        AcceptanceScenario.CARE_FOLLOWUP: "care",
    }.get(scenario)
    if state_kind is None:
        return ()
    return (
        StatePersistenceAcceptanceReceipt(
            state_kind=state_kind,
            subject_ref_hash=stable_hash(
                {
                    "subject": scenario.value,
                    "repetition": repetition,
                }
            ),
            version_before=repetition - 1,
            version_after=repetition,
            commit_receipt_id=(
                f"state-commit:{scenario.value}:{repetition}"
            ),
            restarted_at=datetime(
                2026,
                7,
                27,
                repetition,
                tzinfo=timezone.utc,
            ),
        ),
    )


def approved_domain_review() -> HabitDomainReviewReport:
    reviewer = HabitDomainReviewer(
        reviewer_ref="reviewer:domain-real",
        display_name="Reviewed Professional",
        professional_role="sleep-domain reviewer",
        qualification="verified professional qualification",
        organization_ref="organization:verified-review",
        conflict_of_interest="none_declared",
    )

    def finding(*, eligible: bool | None = None) -> HabitDomainReviewFinding:
        return HabitDomainReviewFinding(
            decision="approved",
            reviewer_refs=(reviewer.reviewer_ref,),
            finding="approved verification finding",
            eligible=eligible,
        )

    reviews = tuple(
        HabitConceptDomainReview(
            concept_id=concept.concept_id,
            version=concept.version,
            content_version=concept.content_version,
            valid_for_days=concept.valid_for_days,
            domain_review_status="approved",
            reviewer_ref=reviewer.reviewer_ref,
            wording_review=finding(),
            options_review=finding(),
            ttl_review=finding(),
            persistence_eligibility_review=finding(
                eligible=concept.persistence.value == "profile_eligible"
            ),
            safety_review=finding(),
            final_decision="approved",
            reviewed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
            approval_record_ref=f"approval:{concept.concept_id}:{concept.version}",
        )
        for concept in DEFAULT_HABIT_CONCEPTS
    )
    return HabitDomainReviewReport(
        evidence_kind=AcceptanceEvidenceKind.REAL,
        review_report_id="habit-domain-review:test",
        review_type="catalog_wording_options_ttl_persistence",
        reviewed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        catalog=HabitDomainReviewCatalog(
            catalog_id="habit-question-catalog",
            catalog_version="1.0.0",
            catalog_hash=current_habit_catalog_hash(),
            content_version=DEFAULT_HABIT_CONCEPTS[0].content_version,
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
        concept_reviews=reviews,
        overall_decision="approved",
        overall_findings=("approved verification",),
        approval_reference="approval-bundle:test",
        signoff=HabitDomainReviewSignoff(
            status="approved",
            signed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
            signed_by=(reviewer.reviewer_ref,),
            signature_reference="signature:test",
        ),
    )


def test_acceptance_identity_binds_all_runtime_versions_and_registry_hash() -> None:
    identity = current_acceptance_release_identity()
    assert identity.registry_hash == stable_hash(product_agent_manifest())
    assert identity.acceptance_manifest_version == ACCEPTANCE_MANIFEST_VERSION
    assert (
        identity.product_api_adapter_version
        == PRODUCT_EPISODE_API_ADAPTER_VERSION
    )
    assert (
        identity.product_external_action_version
        == PRODUCT_EXTERNAL_ACTION_VERSION
    )
    assert (
        identity.product_state_persistence_version
        == PRODUCT_STATE_PERSISTENCE_VERSION
    )
    assert len(identity.identity_hash) == 64


def test_checked_in_manifest_matches_current_identity_and_starts_fail_closed() -> None:
    path = Path(__file__).parents[2] / "docs" / "audits" / "agent-architecture" / "ACCEPTANCE-MANIFEST.json"
    raw = json.loads(path.read_text())
    manifest = AcceptanceManifest.model_validate(raw)
    assert manifest.release_identity == current_acceptance_release_identity()
    report = ProductAgentReleaseVerifier().verify(manifest)
    assert not report.eligible
    assert set(report.missing_scenarios) == set(AcceptanceScenario)


def test_release_gate_requires_all_scenarios_three_real_runs_and_domain_review() -> None:
    identity = current_acceptance_release_identity("candidate")
    observations = []
    for scenario in AcceptanceScenario:
        repetitions = (
            [1]
            if scenario
            in {AcceptanceScenario.DATA_QUALITY, AcceptanceScenario.URGENT}
            else [1, 2, 3]
        )
        for repetition in repetitions:
            observations.append(
                AcceptanceObservation(
                    observation_id=f"{scenario.value}:{repetition}",
                    scenario=scenario,
                    release_identity_hash=identity.identity_hash,
                    repetition=repetition,
                    role="elder",
                    observed_agents=[AgentId.SLEEP_CARE],
                    evidence_kind=AcceptanceEvidenceKind.REAL,
                    real_provider=scenario
                    not in {
                        AcceptanceScenario.DATA_QUALITY,
                        AcceptanceScenario.URGENT,
                    },
                    provider_receipt=(
                        None
                        if scenario
                        in {
                            AcceptanceScenario.DATA_QUALITY,
                            AcceptanceScenario.URGENT,
                        }
                        else provider_receipt(scenario, repetition)
                    ),
                    runtime_receipt=runtime_receipt(
                        scenario,
                        repetition,
                    ),
                    external_action_receipt=(
                        external_action_receipt(repetition)
                        if scenario == AcceptanceScenario.EXTERNAL_ACTION
                        else None
                    ),
                    state_persistence_receipts=(
                        state_persistence_receipts(
                            scenario,
                            repetition,
                        )
                    ),
                    domain_reviewed=scenario
                    == AcceptanceScenario.NORMAL_MORNING,
                    passed=True,
                )
            )
    manifest = AcceptanceManifest(
        release_version="candidate",
        release_identity=identity,
        observations=observations,
        habit_domain_review_report=approved_domain_review(),
        habit_usability_report=HabitUsabilityReport(
            evidence_kind=AcceptanceEvidenceKind.REAL,
            report_id="usability:test",
            conducted_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
            observations=[
                HabitUsabilityObservation(
                    participant_ref=f"participant:{index}",
                    age_band="70-79",
                    understood_personalization_purpose=True,
                    understood_questions_are_skippable=True,
                    understood_separate_persistence_confirmation=True,
                    skip_attempt_succeeded=True,
                    questions_presented=2,
                    erroneous_confirmation=False,
                    interruption_rating=2,
                )
                for index in range(3)
            ],
            reviewer_ref="reviewer:test",
            attestation=HabitUsabilityAttestation(
                protocol_version="sleep-habit-usability.v1",
                facilitator_ref="facilitator:verified",
                observed_participant_refs=tuple(
                    f"participant:{index}" for index in range(3)
                ),
                participant_interactions_observed=True,
                synthetic_data_used=False,
                signed_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                signature_reference="signature:usability:verified",
            ),
        ),
    )
    assert ProductAgentReleaseVerifier().verify(manifest).eligible


def test_any_hard_violation_blocks_release() -> None:
    manifest = empty_manifest()
    manifest.observations.append(
        AcceptanceObservation(
            observation_id="bad",
            scenario=AcceptanceScenario.NORMAL_MORNING,
            release_identity_hash=manifest.release_identity.identity_hash,
            repetition=1,
            role="elder",
            observed_agents=[AgentId.SLEEP_CARE],
            evidence_kind=AcceptanceEvidenceKind.REAL,
            hard_violations=[HardViolation.SAFETY_BYPASS],
            passed=True,
        )
    )
    report = ProductAgentReleaseVerifier().verify(manifest)
    assert HardViolation.SAFETY_BYPASS in report.violations
    with pytest.raises(ReleaseGateError):
        ProductAgentReleaseVerifier().require_eligible(manifest)


def test_release_gate_requires_runtime_external_and_restart_proofs() -> None:
    identity = current_acceptance_release_identity()
    manifest = empty_manifest()
    manifest.observations.extend(
        [
            AcceptanceObservation(
                observation_id="external_action:missing-gateway",
                scenario=AcceptanceScenario.EXTERNAL_ACTION,
                release_identity_hash=identity.identity_hash,
                repetition=1,
                role="elder",
                observed_agents=[AgentId.SLEEP_CARE],
                evidence_kind=AcceptanceEvidenceKind.REAL,
                real_provider=True,
                provider_receipt=provider_receipt(
                    AcceptanceScenario.EXTERNAL_ACTION,
                    1,
                ),
                runtime_receipt=runtime_receipt(
                    AcceptanceScenario.EXTERNAL_ACTION,
                    1,
                ),
                passed=True,
            ),
            AcceptanceObservation(
                observation_id="memory:missing-restart",
                scenario=AcceptanceScenario.MEMORY,
                release_identity_hash=identity.identity_hash,
                repetition=1,
                role="elder",
                observed_agents=[AgentId.SLEEP_CARE],
                evidence_kind=AcceptanceEvidenceKind.REAL,
                real_provider=True,
                provider_receipt=provider_receipt(
                    AcceptanceScenario.MEMORY,
                    1,
                ),
                runtime_receipt=runtime_receipt(
                    AcceptanceScenario.MEMORY,
                    1,
                ),
                passed=True,
            ),
            AcceptanceObservation(
                observation_id="trend:missing-runtime",
                scenario=AcceptanceScenario.TREND,
                release_identity_hash=identity.identity_hash,
                repetition=1,
                role="elder",
                observed_agents=[AgentId.SLEEP_CARE],
                evidence_kind=AcceptanceEvidenceKind.REAL,
                real_provider=True,
                provider_receipt=provider_receipt(
                    AcceptanceScenario.TREND,
                    1,
                ),
                passed=True,
            ),
        ]
    )

    report = ProductAgentReleaseVerifier().verify(manifest)

    assert "real observations lack bound runtime receipts" in report.reasons
    assert (
        "external_action lacks confirmed gateway execution receipts"
        in report.reasons
    )
    assert (
        "memory lacks restart-verified memory persistence"
        in report.reasons
    )


def test_supplied_simulated_materials_cannot_unlock_release() -> None:
    root = Path(__file__).parents[2] / "tests" / "fixtures" / "acceptance-materials"
    usability_raw = json.loads(
        (root / "habit_usability_report.simulated.json").read_text()
    )
    domain_raw = json.loads(
        (root / "habit_domain_review.simulated.json").read_text()
    )
    usability = HabitUsabilityReport.model_validate(usability_raw)
    domain_review = HabitDomainReviewReport.model_validate(domain_raw)
    provider_rows = json.loads(
        (root / "provider_observations.simulated.json").read_text()
    )["observations"]

    assert usability.evidence_kind == AcceptanceEvidenceKind.SIMULATED
    assert domain_review.evidence_kind == AcceptanceEvidenceKind.SIMULATED
    assert domain_review.current_catalog_errors()
    with pytest.raises(ValidationError, match="simulation marker"):
        HabitUsabilityReport.model_validate(
            {**usability_raw, "evidence_kind": "real"}
        )
    with pytest.raises(ValidationError, match="facilitator attestation"):
        HabitUsabilityReport.model_validate(
            {
                **usability_raw,
                "evidence_kind": "real",
                "report_id": "habit-usability:verified",
                "reviewer_ref": "reviewer:verified",
                "observations": [
                    {
                        **item,
                        "participant_ref": f"participant:verified-{index}",
                    }
                    for index, item in enumerate(
                        usability_raw["observations"]
                    )
                ],
            }
        )
    with pytest.raises(ValidationError, match="simulation marker"):
        HabitDomainReviewReport.model_validate(
            {**domain_raw, "evidence_kind": "real"}
        )
    with pytest.raises(
        ValidationError, match="simulated observation cannot claim a real provider"
    ):
        AcceptanceObservation.model_validate(provider_rows[0])

    manifest = empty_manifest().model_copy(
        update={
            "habit_domain_review_report": domain_review,
            "habit_usability_report": usability,
        }
    )
    report = ProductAgentReleaseVerifier().verify(manifest)
    assert not report.eligible
    assert "Habit domain review report is not real evidence" in report.reasons
    assert "Habit usability report is not real evidence" in report.reasons


def test_simulated_material_audit_reports_exact_drift_without_rewriting_it() -> None:
    root = Path(__file__).parents[2] / "tests" / "fixtures" / "acceptance-materials"
    report = audit_acceptance_materials(root, manifest=empty_manifest())
    codes = {item.code for item in report.findings}

    assert report.usability_observations == 5
    assert report.domain_concepts == 12
    assert report.provider_observations == 68
    assert report.usable_as_simulation_fixture
    assert not report.release_evidence_eligible
    assert {
        "usability.simulated",
        "domain.simulated",
        "domain.catalog_drift",
        "provider.scenario_drift",
        "provider.release_identity_mismatch",
        "provider.unverifiable_receipts",
    }.issubset(codes)


def test_material_audit_reuses_complete_release_gate_for_real_package(
    tmp_path: Path,
) -> None:
    identity = current_acceptance_release_identity()
    observations = []
    for scenario in AcceptanceScenario:
        repetitions = (
            (1,)
            if scenario
            in {AcceptanceScenario.DATA_QUALITY, AcceptanceScenario.URGENT}
            else (1, 2, 3)
        )
        for repetition in repetitions:
            deterministic = scenario in {
                AcceptanceScenario.DATA_QUALITY,
                AcceptanceScenario.URGENT,
            }
            observations.append(
                AcceptanceObservation(
                    observation_id=f"{scenario.value}:{repetition}",
                    scenario=scenario,
                    release_identity_hash=identity.identity_hash,
                    repetition=repetition,
                    role="elder",
                    observed_agents=[AgentId.SLEEP_CARE],
                    evidence_kind=AcceptanceEvidenceKind.REAL,
                    real_provider=not deterministic,
                    provider_receipt=(
                        None
                        if deterministic
                        else provider_receipt(scenario, repetition)
                    ),
                    runtime_receipt=runtime_receipt(
                        scenario,
                        repetition,
                    ),
                    external_action_receipt=(
                        external_action_receipt(repetition)
                        if scenario == AcceptanceScenario.EXTERNAL_ACTION
                        else None
                    ),
                    state_persistence_receipts=(
                        state_persistence_receipts(
                            scenario,
                            repetition,
                        )
                    ),
                    passed=True,
                )
            )
    usability = HabitUsabilityReport(
        evidence_kind=AcceptanceEvidenceKind.REAL,
        report_id="usability:verified",
        conducted_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        observations=[
            HabitUsabilityObservation(
                participant_ref=f"participant:{index}",
                age_band="70-79",
                understood_personalization_purpose=True,
                understood_questions_are_skippable=True,
                understood_separate_persistence_confirmation=True,
                skip_attempt_succeeded=True,
                questions_presented=2,
                erroneous_confirmation=False,
                interruption_rating=2,
            )
            for index in range(3)
        ],
        reviewer_ref="reviewer:verified-ux",
        attestation=HabitUsabilityAttestation(
            protocol_version="sleep-habit-usability.v1",
            facilitator_ref="facilitator:verified",
            observed_participant_refs=tuple(
                f"participant:{index}" for index in range(3)
            ),
            participant_interactions_observed=True,
            synthetic_data_used=False,
            signed_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
            signature_reference="signature:usability:verified",
        ),
    )
    (tmp_path / "habit_usability_report.json").write_text(
        usability.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (tmp_path / "habit_domain_review.json").write_text(
        approved_domain_review().model_dump_json(indent=2),
        encoding="utf-8",
    )
    (tmp_path / "provider_observations.json").write_text(
        json.dumps(
            {
                "observations": [
                    item.model_dump(mode="json") for item in observations
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    report = audit_acceptance_materials(
        tmp_path,
        manifest=empty_manifest(),
    )
    assert report.release_evidence_eligible
    assert report.provider_observations == 68
    assert report.findings == ()


def test_material_template_initializer_binds_current_catalog_and_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acceptance"
    manifest = empty_manifest()

    result = initialize_acceptance_materials(root, manifest=manifest)

    assert result.release_identity_hash == manifest.release_identity.identity_hash
    assert result.habit_catalog_hash == current_habit_catalog_hash()
    assert result.habit_concepts == len(DEFAULT_HABIT_CONCEPTS)
    assert result.provider_observation_slots == 68
    assert not result.release_evidence_eligible
    assert set(result.files) == {
        "habit_usability_report.json",
        "habit_domain_review.json",
        "provider_observations.json",
        "README.md",
    }

    domain = HabitDomainReviewReport.model_validate_json(
        (root / "habit_domain_review.json").read_text()
    )
    provider_raw = json.loads(
        (root / "provider_observations.json").read_text()
    )
    assert domain.current_catalog_errors() == ()
    assert len(provider_raw["observations"]) == 68
    assert {
        item["scenario"] for item in provider_raw["observations"]
    } == {scenario.value for scenario in AcceptanceScenario}
    assert {
        item["release_identity_hash"]
        for item in provider_raw["observations"]
    } == {manifest.release_identity.identity_hash}

    audit = audit_acceptance_materials(root, manifest=manifest)
    codes = {item.code for item in audit.findings}
    assert audit.usable_as_simulation_fixture
    assert not audit.release_evidence_eligible
    assert "domain.catalog_drift" not in codes
    assert "provider.scenario_drift" not in codes
    assert "provider.release_identity_mismatch" not in codes
    assert {"usability.simulated", "domain.simulated", "release.gate_failed"} <= codes


def test_checked_in_v23_collection_skeleton_is_current_and_fail_closed() -> None:
    repository = Path(__file__).parents[2]
    root = (
        repository
        / "docs" / "product" / "habit-profile"
        / "real-evidence-v23-collection"
    )
    manifest = AcceptanceManifest.model_validate_json(
        (
            repository / "docs" / "audits" / "agent-architecture" / "ACCEPTANCE-MANIFEST.json"
        ).read_text()
    )

    audit = audit_acceptance_materials(root, manifest=manifest)
    codes = {item.code for item in audit.findings}

    assert audit.usability_observations == 3
    assert audit.domain_concepts == len(DEFAULT_HABIT_CONCEPTS)
    assert audit.provider_observations == 68
    assert audit.usable_as_simulation_fixture
    assert not audit.release_evidence_eligible
    assert "provider.release_identity_mismatch" not in codes
    assert "provider.scenario_drift" not in codes
    assert "domain.catalog_drift" not in codes
    assert {"usability.simulated", "domain.simulated", "release.gate_failed"} <= codes


def test_material_templates_cannot_be_promoted_by_flipping_evidence_kind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acceptance"
    initialize_acceptance_materials(root, manifest=empty_manifest())
    usability_raw = json.loads(
        (root / "habit_usability_report.json").read_text()
    )
    domain_raw = json.loads((root / "habit_domain_review.json").read_text())
    provider_raw = json.loads(
        (root / "provider_observations.json").read_text()
    )

    with pytest.raises(ValidationError, match="simulation marker"):
        HabitUsabilityReport.model_validate(
            {**usability_raw, "evidence_kind": "real"}
        )
    with pytest.raises(ValidationError, match="simulation marker"):
        HabitDomainReviewReport.model_validate(
            {**domain_raw, "evidence_kind": "real"}
        )
    with pytest.raises(ValidationError, match="simulation marker"):
        AcceptanceObservation.model_validate(
            {
                **provider_raw["observations"][0],
                "evidence_kind": "real",
            }
        )


def test_material_template_initializer_refuses_overwrite_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acceptance"
    root.mkdir()
    existing = root / "habit_usability_report.json"
    existing.write_text('{"owned_by":"user"}\n')

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        initialize_acceptance_materials(root, manifest=empty_manifest())

    assert existing.read_text() == '{"owned_by":"user"}\n'
    assert not (root / "habit_domain_review.json").exists()
    assert not (root / "provider_observations.json").exists()
    assert not (root / "README.md").exists()


def test_current_complete_simulated_fixture_is_structurally_valid_but_ineligible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "simulated-acceptance"
    manifest = empty_manifest()

    result = initialize_simulated_acceptance_materials(
        root,
        manifest=manifest,
    )
    report = audit_acceptance_materials(root, manifest=manifest)
    usability = HabitUsabilityReport.model_validate_json(
        (root / "habit_usability_report.json").read_text()
    )
    domain = HabitDomainReviewReport.model_validate_json(
        (root / "habit_domain_review.json").read_text()
    )
    provider_raw = json.loads(
        (root / "provider_observations.json").read_text()
    )
    observations = [
        AcceptanceObservation.model_validate(item)
        for item in provider_raw["observations"]
    ]
    receipts = [
        item.provider_receipt
        for item in observations
        if item.provider_receipt is not None
    ]
    request_ids = [
        request_id
        for receipt in receipts
        for request_id in receipt.provider_request_ids
    ]
    codes = {item.code for item in report.findings}

    assert result.release_identity_hash == manifest.release_identity.identity_hash
    assert result.habit_catalog_hash == current_habit_catalog_hash()
    assert result.usability_observations == 5
    assert result.habit_concepts == len(DEFAULT_HABIT_CONCEPTS)
    assert result.provider_observations == 68
    assert usability.attestation is not None
    assert usability.attestation.synthetic_data_used
    assert len(usability.attestation.observed_participant_refs) == 5
    assert len(domain.reviewers) == 2
    assert domain.overall_decision == "approved"
    assert domain.current_catalog_errors() == ()
    assert len(observations) == 68
    assert len(receipts) == 66
    assert len(request_ids) == 66
    assert len(set(request_ids)) == 66
    assert {
        item.scenario
        for item in observations
        if item.provider_receipt is None
    } == {
        AcceptanceScenario.DATA_QUALITY,
        AcceptanceScenario.URGENT,
    }
    assert all(item.evidence_kind == AcceptanceEvidenceKind.SIMULATED for item in observations)
    assert all(not item.real_provider for item in observations)
    assert {
        item.release_identity_hash for item in observations
    } == {manifest.release_identity.identity_hash}
    assert report.usable_as_simulation_fixture
    assert not report.release_evidence_eligible
    assert codes == {
        "usability.simulated",
        "domain.simulated",
        "release.gate_failed",
    }


def test_checked_in_v18_simulation_coverage_inventory_matches_artifacts(
    tmp_path: Path,
) -> None:
    inventory = json.loads(
        (
            REPOSITORY
            / "docs" / "product" / "habit-profile"
            / "SIMULATION-COVERAGE-v18.json"
        ).read_text()
    )
    material_root = REPOSITORY / inventory["material_root"]
    coverage = inventory["coverage"]

    for filename, expected_hash in inventory["canonical_file_sha256"].items():
        assert hashlib.sha256(
            (material_root / filename).read_bytes()
        ).hexdigest() == expected_hash
    archive_metadata = inventory["archive"]
    assert archive_metadata["distribution"] == "generated_not_committed"
    _build_fixture_archive(
        tmp_path,
        material_root=material_root,
        archive_root=archive_metadata["archive_root"],
        expected_sha256=archive_metadata["sha256"],
    )
    assert inventory["purpose"] == "development_test_only"
    assert not inventory["production_release_eligible"]
    assert inventory["must_not_be_promoted_to_real_evidence"]
    assert coverage == {
        "simulated_usability_observations": 5,
        "synthetic_facilitator_attestations": 1,
        "simulated_named_reviewer_personas": 2,
        "reviewed_habit_concepts": 10,
        "simulated_provider_observations": 68,
        "synthetic_provider_receipts": 66,
        "synthetic_provider_request_ids": 66,
        "deterministic_observations_without_receipts": 2,
    }


def test_simulated_fixture_initializer_refuses_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "simulated-acceptance"
    initialize_simulated_acceptance_materials(
        root,
        manifest=empty_manifest(),
    )

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        initialize_simulated_acceptance_materials(
            root,
            manifest=empty_manifest(),
        )


def test_acceptance_archive_builder_is_byte_stable(tmp_path: Path) -> None:
    root = tmp_path / "simulated-acceptance"
    initialize_simulated_acceptance_materials(
        root,
        manifest=empty_manifest(),
    )

    first = build_acceptance_material_archive(
        root,
        tmp_path / "first.zip",
        archive_root="sleepagent-current-simulated-evidence",
    )
    second = build_acceptance_material_archive(
        root,
        tmp_path / "second.zip",
        archive_root="sleepagent-current-simulated-evidence",
    )

    assert first.sha256 == second.sha256
    assert (tmp_path / "first.zip").read_bytes() == (
        tmp_path / "second.zip"
    ).read_bytes()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_acceptance_material_archive(
            root,
            tmp_path / "first.zip",
            archive_root="sleepagent-current-simulated-evidence",
        )


def test_v24_current_simulation_inventory_binds_22_concept_catalog(
    tmp_path: Path,
) -> None:
    inventory = json.loads(
        (
            REPOSITORY
            / "docs" / "product" / "habit-profile"
            / "SIMULATION-COVERAGE-v24.json"
        ).read_text()
    )
    material_root = REPOSITORY / inventory["material_root"]
    for filename, expected_hash in inventory["canonical_file_sha256"].items():
        assert hashlib.sha256(
            (material_root / filename).read_bytes()
        ).hexdigest() == expected_hash
    archive_metadata = inventory["archive"]
    assert archive_metadata["distribution"] == "generated_not_committed"
    archive = _build_fixture_archive(
        tmp_path,
        material_root=material_root,
        archive_root=archive_metadata["archive_root"],
        expected_sha256=archive_metadata["sha256"],
    )

    report = audit_acceptance_material_archive(
        archive,
        manifest=empty_manifest(),
    )
    codes = {item.code for item in report.findings}
    assert inventory["release_identity_hash"] == (
        current_acceptance_release_identity().identity_hash
    )
    assert inventory["habit_catalog_hash"] == current_habit_catalog_hash()
    assert inventory["coverage"]["reviewed_habit_concepts"] == len(
        DEFAULT_HABIT_CONCEPTS
    )
    assert report.domain_concepts == len(DEFAULT_HABIT_CONCEPTS)
    assert report.usable_as_simulation_fixture
    assert not report.release_evidence_eligible
    assert codes == {
        "usability.simulated",
        "domain.simulated",
        "release.gate_failed",
    }


def test_v24_current_simulation_fixture_matches_current_initializer(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "generated-current-fixture"
    initialize_simulated_acceptance_materials(
        generated,
        manifest=empty_manifest(),
    )
    checked_in = (
        REPOSITORY
        / "docs" / "product" / "habit-profile"
        / "simulated-evidence-v24-current"
    )

    for filename in (
        "habit_usability_report.json",
        "habit_domain_review.json",
        "provider_observations.json",
        "README.md",
    ):
        assert (generated / filename).read_bytes() == (
            checked_in / filename
        ).read_bytes()


def test_v15_template_archive_is_a_bounded_historical_fixture(
    tmp_path: Path,
) -> None:
    archive = _build_fixture_archive(
        tmp_path,
        material_root=(
            REPOSITORY
            / "docs" / "product" / "habit-profile"
            / "simulated-evidence-v15-20260726-132754"
        ),
        archive_root="sleepagent-v15-historical-template",
        expected_sha256=(
            "b4b6c02e2342ef57ef910e66d33a6e6fd6d2d8724b4f2892c611a4fd8cc47315"
        ),
    )

    report = audit_acceptance_material_archive(
        archive,
        manifest=empty_manifest(),
    )
    codes = {item.code for item in report.findings}

    assert report.material_root == str(archive)
    assert report.usability_observations == 3
    assert report.domain_concepts == 10
    assert report.provider_observations == 68
    assert report.usable_as_simulation_fixture
    assert not report.release_evidence_eligible
    assert codes == {
        "usability.simulated",
        "domain.simulated",
        "domain.catalog_drift",
        "provider.release_identity_mismatch",
        "release.gate_failed",
    }


def test_v18_simulated_receipt_and_attestation_are_honestly_parseable(
    tmp_path: Path,
) -> None:
    inventory = json.loads(
        (
            REPOSITORY
            / "docs" / "product" / "habit-profile"
            / "SIMULATION-COVERAGE-v18.json"
        ).read_text()
    )
    archive_metadata = inventory["archive"]
    archive = _build_fixture_archive(
        tmp_path,
        material_root=REPOSITORY / inventory["material_root"],
        archive_root=archive_metadata["archive_root"],
        expected_sha256=archive_metadata["sha256"],
    )
    with ZipFile(archive) as bundle:
        usability_raw = json.loads(
            bundle.read(
                "sleepagent-v18-complete-simulated-evidence/"
                "habit_usability_report.json"
            )
        )
        provider_raw = json.loads(
            bundle.read(
                "sleepagent-v18-complete-simulated-evidence/"
                "provider_observations.json"
            )
        )

    usability = HabitUsabilityReport.model_validate(usability_raw)
    simulated_provider = AcceptanceObservation.model_validate(
        next(
            item
            for item in provider_raw["observations"]
            if item["provider_receipt"] is not None
        )
    )
    assert usability.attestation is not None
    assert usability.attestation.synthetic_data_used
    assert not simulated_provider.real_provider
    assert simulated_provider.provider_receipt is not None

    with pytest.raises(ValidationError, match="cannot use synthetic data"):
        HabitUsabilityReport.model_validate(
            {
                **usability_raw,
                "evidence_kind": "real",
                "report_id": "habit-usability:actual",
                "reviewer_ref": "reviewer:actual",
                "observations": [
                    {
                        **item,
                        "participant_ref": f"participant:actual-{index}",
                    }
                    for index, item in enumerate(
                        usability_raw["observations"],
                        start=1,
                    )
                ],
                "attestation": {
                    **usability_raw["attestation"],
                    "facilitator_ref": "facilitator:actual",
                    "observed_participant_refs": [
                        f"participant:actual-{index}"
                        for index in range(1, 6)
                    ],
                    "signature_reference": "signature:actual",
                },
            }
        )
    with pytest.raises(
        ValidationError,
        match="real deterministic observation cannot carry a provider receipt",
    ):
        AcceptanceObservation.model_validate(
            {
                **simulated_provider.model_dump(mode="json"),
                "evidence_kind": "real",
                "observation_id": "actual:normal_morning:1",
            }
        )


def test_acceptance_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("../habit_usability_report.json", "{}")

    report = audit_acceptance_material_archive(
        archive,
        manifest=empty_manifest(),
    )

    assert not report.usable_as_simulation_fixture
    assert not report.release_evidence_eligible
    assert [item.code for item in report.findings] == ["archive.invalid"]
    assert "unsafe archive path" in report.findings[0].message
