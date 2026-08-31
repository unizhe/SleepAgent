from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.domain.contracts import (
    AnalysisRevision,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisStatus,
    DataMode,
    DataSufficiency,
    RoleViewStatus,
)
from sleepagent.application.product_data import public_product_subject_ref
from sleepagent.workers.product import (
    PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST,
    ProductAgentInvariantError,
    _public_today_projection,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
SHA = "a" * 64
SUBJECT = "private-real-subject-identifier"


def _analysis() -> AnalysisRevision:
    return AnalysisRevision(
        analysis_revision_id="analysis-public-resource-1",
        night_episode_id="night-public-resource-1",
        night_episode_revision_id="night-revision-public-resource-1",
        night_episode_revision_number=1,
        data_mode=DataMode.LIVE,
        subject_id=SUBJECT,
        revision_number=1,
        analysis_run_id="private-product-execution-1",
        observation_set_sha256=SHA,
        adapter_versions={"perceptor": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"product": "1.0.0"},
        data_sufficiency=DataSufficiency.DATA_INSUFFICIENT,
        status=AnalysisStatus.DEGRADED,
        execution_mode="safe_degraded",
        failure_codes=("sleepcare_planning_failed:EpisodePlanPolicyError",),
        result_resource_id="analysis-public-resource-1",
        created_at=NOW,
    )


def _view(
    role: AnalysisRole,
    *,
    status: RoleViewStatus = RoleViewStatus.READY,
) -> AnalysisRoleView:
    blocked = status == RoleViewStatus.BLOCKED
    return AnalysisRoleView(
        role_view_id=f"projection-public-resource-{role.value}",
        analysis_revision_id="analysis-public-resource-1",
        night_episode_id="night-public-resource-1",
        night_episode_revision_id="night-revision-public-resource-1",
        data_mode=DataMode.LIVE,
        subject_id=SUBJECT,
        role=role,
        status=status,
        product_agent_episode_id="private-product-execution-1",
        execution_mode="safe_degraded" if blocked else "intelligent",
        content=None if blocked else "Authorized partial-quality sleep summary.",
        context_notice=(
            None if blocked else "Some source coverage was unavailable."
        ),
        claim_refs=("private-claim-database-id",),
        source_refs=(
            "private-device-identifier",
            "/private/evidence/source.json",
        ),
        failure_codes=("sleepcare_planning_failed:EpisodePlanPolicyError",)
        if blocked
        else (),
        generated_at=NOW,
    )


def _project(view: AnalysisRoleView) -> dict:
    return _public_today_projection(
        analysis=_analysis(),
        view=view,
        projection_version=1,
        episode_local_date=date(2026, 8, 24),
        assignment_basis="observed_wake",
        committed_at=NOW,
    )


def test_public_projection_pseudonymizes_subject_and_enforces_allowlist() -> None:
    payload = _project(_view(AnalysisRole.ELDER))

    assert set(payload) == PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST
    assert payload["subject_ref"] == public_product_subject_ref(SUBJECT)
    assert SUBJECT not in str(payload)
    assert payload["content"]["summary_text"] == (
        "Authorized partial-quality sleep summary."
    )
    assert payload["content"]["context_notice"] == (
        "Some source coverage was unavailable."
    )


def test_public_projection_filters_internal_and_private_evidence_metadata() -> None:
    view = _view(AnalysisRole.FAMILY)
    internal_before = view.model_dump(mode="json")

    payload = _project(view)

    encoded = str(payload)
    assert "private-product-execution-1" not in encoded
    assert "private-claim-database-id" not in encoded
    assert "private-device-identifier" not in encoded
    assert "/private/evidence/source.json" not in encoded
    assert "source_refs" not in encoded
    assert "claim_refs" not in encoded
    assert view.model_dump(mode="json") == internal_before
    assert view.subject_id == SUBJECT
    assert view.source_refs == (
        "private-device-identifier",
        "/private/evidence/source.json",
    )


def test_role_specific_projection_keeps_doctor_blocked_without_private_refs() -> None:
    doctor = _project(
        _view(AnalysisRole.DOCTOR, status=RoleViewStatus.BLOCKED)
    )
    family = _project(_view(AnalysisRole.FAMILY))

    assert doctor["state"] == "blocked"
    assert doctor["content"]["evidence_refs"] == []
    assert doctor["content"]["summary_text"] == (
        "The sleep summary is currently unavailable."
    )
    assert "evidence_refs" not in family["content"]


def test_reprojection_is_deterministic_and_does_not_mutate_inputs() -> None:
    analysis = _analysis()
    view = _view(AnalysisRole.ELDER)
    analysis_before = analysis.model_dump(mode="json")
    view_before = view.model_dump(mode="json")

    first = _public_today_projection(
        analysis=analysis,
        view=view,
        projection_version=1,
        episode_local_date=date(2026, 8, 24),
        assignment_basis="observed_wake",
        committed_at=NOW,
    )
    second = _public_today_projection(
        analysis=analysis,
        view=view,
        projection_version=1,
        episode_local_date=date(2026, 8, 24),
        assignment_basis="observed_wake",
        committed_at=NOW,
    )

    assert first == second
    assert analysis.model_dump(mode="json") == analysis_before
    assert view.model_dump(mode="json") == view_before


def test_pending_internal_role_view_cannot_be_publicly_projected() -> None:
    pending = _view(AnalysisRole.ELDER).model_copy(
        update={"status": RoleViewStatus.PENDING, "execution_mode": "pending"}
    )

    with pytest.raises(ProductAgentInvariantError, match="pending role views"):
        _project(pending)
