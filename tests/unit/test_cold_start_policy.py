from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.runtime.cold_start import (
    FIXTURE_BASELINE_POLICY_VERSION,
    BaselineMaturity,
    CanonicalMetricNight,
    CapabilityEligibilityReceipt,
    ClaimCeiling,
    ClaimKind,
    ColdStartReason,
    MeasurementCohort,
    MetricReadinessDecision,
    ObjectiveBaselineArtifact,
    ResponseMode,
    build_unavailable_entry_decisions,
    degraded_boundary_sentence,
    derive_metric_valid_nights,
    evaluate_readiness,
    load_baseline_policy,
    project_baseline_readiness,
    resolve_claim_requirement,
    snapshot_binding_material,
)
from sleepagent.runtime.contracts import (
    AgentEnvelope,
    AgentId,
    AuthenticatedBinding,
    CommunicationDraft,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    WorkProductStatus,
)
from sleepagent.runtime.governance import (
    AcceptanceError,
    PublicationError,
    accept_evidence,
    build_cold_start_receipt,
    publication_postflight,
)
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def cohort(
    metric_id: str = "heart_rate_bpm",
    *,
    subject_id: str = "subject-1",
    device_version: str = "binding-1",
    adapter_version: str = "1.0.0",
) -> MeasurementCohort:
    values = {
        "subject_id": subject_id,
        "metric_id": metric_id,
        "data_mode": "live",
        "device_binding_version": device_version,
        "device_measurement_domain": "radar-vitals.v1",
        "adapter_id": "perceptor",
        "adapter_version": adapter_version,
        "adapter_configuration_fingerprint": "a" * 64,
        "observation_schema_version": "sleep-observation.v1",
        "canonical_data_version": "canonical.v1",
        "producer_id": "radar-night-analysis",
        "producer_version": "1.0.0",
        "calibration_state": "unknown",
    }
    if metric_id in {"sleep_minutes", "in_bed_minutes"}:
        values.update(
            {
                "timezone_name": "Asia/Shanghai",
                "sleep_day_policy_version": "wake-date.v1",
                "boundary_policy_version": "radar-boundary.v1",
            }
        )
    return MeasurementCohort.model_validate(values)


def capability(
    capability_id: str = "adapter.realtime_vitals",
    *,
    version: str = "1.0.0",
) -> CapabilityEligibilityReceipt:
    return CapabilityEligibilityReceipt.create(
        receipt_id=f"eligible:{capability_id}",
        registry_kind="adapter",
        registry_snapshot_ref="adapter-registry:lock-1",
        registry_snapshot_hash="b" * 64,
        capability_id=capability_id,
        capability_version=version,
        environment="test",
        configuration_or_content_hash="a" * 64,
        eligible=True,
        reason_code="verified_and_enabled",
        authoritative_refs=(
            "adapter-resolution-lock:lock-1",
            "capability-verification:verification-1",
            "adapter-deployment:enable-1",
        ),
        resolved_at=NOW,
    )


def night(
    sleep_date: date,
    *,
    metric_id: str = "heart_rate_bpm",
    measurement_cohort: MeasurementCohort | None = None,
    revision: int = 1,
    metric_present: bool = True,
    coverage: float = 0.9,
    quality: str = "usable",
    blocking_flags: tuple[str, ...] = (),
) -> CanonicalMetricNight:
    measurement_cohort = measurement_cohort or cohort(metric_id)
    return CanonicalMetricNight(
        night_episode_id=f"night:{sleep_date.isoformat()}",
        night_episode_revision_id=(
            f"night:{sleep_date.isoformat()}:revision:{revision}"
        ),
        revision_number=revision,
        subject_id=measurement_cohort.subject_id,
        metric_id=metric_id,
        local_sleep_date=sleep_date,
        cohort=measurement_cohort,
        night_eligible=True,
        metric_present=metric_present,
        coverage_ratio=coverage,
        quality_status=quality,
        blocking_flags=blocking_flags,
        source_refs=(
            f"night-revision:{sleep_date.isoformat()}:{revision}",
        ),
        observed_at=NOW,
    )


def count(
    number: int,
    *,
    measurement_cohort: MeasurementCohort | None = None,
    metric_id: str = "heart_rate_bpm",
):
    measurement_cohort = measurement_cohort or cohort(metric_id)
    policy = load_baseline_policy(metric_id, environment="test")
    nights = [
        night(
            NOW.date() - timedelta(days=index),
            metric_id=metric_id,
            measurement_cohort=measurement_cohort,
        )
        for index in range(number)
    ]
    return derive_metric_valid_nights(
        nights,
        metric_id=metric_id,
        cohort=measurement_cohort,
        policy=policy,
        date_start=NOW.date() - timedelta(days=29),
        date_end=NOW.date(),
        as_of=NOW,
    )


def test_catalog_fails_closed_and_explicit_comparison_never_selects_by_value() -> None:
    with pytest.raises(ValueError, match="unknown ClaimRequirement"):
        resolve_claim_requirement("invented:heart_rate_bpm")
    with pytest.raises(ValueError, match="user dates"):
        resolve_claim_requirement("compare_explicit_nights:heart_rate_bpm")

    selected = (date(2026, 7, 28), date(2026, 7, 30))
    requirement = resolve_claim_requirement(
        "compare_explicit_nights:heart_rate_bpm",
        explicit_dates=selected,
    )
    evidence = derive_metric_valid_nights(
        [
            night(date(2026, 7, 28)),
            night(date(2026, 7, 29)),
            night(date(2026, 7, 30)),
        ],
        metric_id="heart_rate_bpm",
        cohort=cohort(),
        policy=load_baseline_policy("heart_rate_bpm", environment="test"),
        selected_dates=requirement.fixed_dates,
        as_of=NOW,
    )
    assert [item.local_sleep_date for item in evidence.valid_nights] == list(
        selected
    )
    with pytest.raises(ValueError, match="explicit dates or one complete window"):
        derive_metric_valid_nights(
            evidence.valid_nights,
            metric_id="heart_rate_bpm",
            cohort=cohort(),
            policy=load_baseline_policy(
                "heart_rate_bpm",
                environment="test",
            ),
            selected_dates=selected,
            date_start=selected[0],
            date_end=selected[-1],
            as_of=NOW,
        )


def test_metric_validity_is_per_metric_and_latest_revision_counts_once() -> None:
    current = cohort()
    old = night(date(2026, 7, 30), revision=1)
    corrected = night(date(2026, 7, 30), revision=2)
    missing = night(
        date(2026, 7, 29),
        metric_present=False,
    )
    incompatible = night(
        date(2026, 7, 28),
        measurement_cohort=cohort(adapter_version="2.0.0"),
    )
    evidence = derive_metric_valid_nights(
        (old, corrected, missing, incompatible),
        metric_id="heart_rate_bpm",
        cohort=current,
        policy=load_baseline_policy("heart_rate_bpm", environment="test"),
        as_of=NOW,
    )
    assert evidence.count == 1
    assert evidence.valid_nights[0].revision_number == 2
    assert ColdStartReason.METRIC_MISSING in evidence.reason_codes
    assert (
        ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT
        in evidence.reason_codes
    )


@pytest.mark.parametrize(
    ("night_count", "ceiling", "mode"),
    [
        (0, ClaimCeiling.GENERAL_KNOWLEDGE, ResponseMode.DEGRADED),
        (1, ClaimCeiling.SINGLE_NIGHT_DESCRIPTION, ResponseMode.DEGRADED),
        (2, ClaimCeiling.SHORT_SERIES_DIFFERENCE, ResponseMode.DEGRADED),
        (4, ClaimCeiling.PROVISIONAL_PATTERN, ResponseMode.DEGRADED),
    ],
)
def test_new_cohort_ceiling_boundaries(
    night_count: int,
    ceiling: ClaimCeiling,
    mode: ResponseMode,
) -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(night_count, measurement_cohort=current)
    projection = project_baseline_readiness(
        artifact=None,
        metric_nights=scoped,
        cohort=current,
        policy=policy,
    )
    decision = evaluate_readiness(
        decision_id=f"decision-{night_count}",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=scoped,
        baseline=projection,
        policy=policy,
        capability_receipts=(capability(),),
    )
    assert decision.claim_ceiling == ceiling
    assert decision.response_mode == mode
    if night_count < 4:
        assert projection.current_maturity == BaselineMaturity.UNAVAILABLE


def test_production_has_no_fixture_policy_and_cannot_auto_promote() -> None:
    assert load_baseline_policy(
        "heart_rate_bpm",
        environment="production",
    ) is None
    current = cohort()
    evidence = count(15, measurement_cohort=current)
    projection = project_baseline_readiness(
        artifact=None,
        metric_nights=evidence,
        cohort=current,
        policy=None,
    )
    assert projection.current_maturity == BaselineMaturity.UNAVAILABLE
    assert (
        ColdStartReason.BASELINE_POLICY_UNAVAILABLE
        in projection.reason_codes
    )


def test_general_knowledge_is_supported_without_becoming_personal() -> None:
    decision = evaluate_readiness(
        decision_id="knowledge",
        requirement=resolve_claim_requirement("general_knowledge"),
        scope_nights=None,
        baseline=None,
        policy=None,
    )
    assert decision.response_mode == ResponseMode.SUPPORTED
    assert decision.claim_ceiling == ClaimCeiling.GENERAL_KNOWLEDGE
    assert decision.metric_id is None
    assert decision.measurement_cohort_ref is None


def test_capability_generation_mismatch_degrades_only_that_claim() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(15, measurement_cohort=current)
    wrong_generation = capability(version="2.0.0")
    decision = evaluate_readiness(
        decision_id="wrong-adapter-generation",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=scoped,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=scoped,
            cohort=current,
            policy=policy,
        ),
        policy=policy,
        capability_receipts=(wrong_generation,),
    )
    assert decision.response_mode == ResponseMode.DEGRADED
    assert decision.claim_ceiling == ClaimCeiling.GENERAL_KNOWLEDGE
    assert (
        ColdStartReason.CAPABILITY_NOT_PRODUCTION_ELIGIBLE
        in decision.reason_codes
    )


def test_coverage_freshness_and_recovery_are_deterministic() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    candidates = [
        night(NOW.date() - timedelta(days=index))
        for index in range(4)
    ]
    candidates[0] = night(NOW.date(), coverage=0.69)
    limited = derive_metric_valid_nights(
        candidates,
        metric_id="heart_rate_bpm",
        cohort=current,
        policy=policy,
        as_of=NOW,
    )
    assert limited.count == 3
    assert ColdStartReason.LIMITED_QUALITY in limited.reason_codes
    assert (
        project_baseline_readiness(
            artifact=None,
            metric_nights=limited,
            cohort=current,
            policy=policy,
        ).current_maturity
        == BaselineMaturity.UNAVAILABLE
    )

    recovered = derive_metric_valid_nights(
        [
            *candidates[1:],
            night(NOW.date(), coverage=0.70),
        ],
        metric_id="heart_rate_bpm",
        cohort=current,
        policy=policy,
        as_of=NOW,
    )
    assert recovered.count == 4
    assert (
        project_baseline_readiness(
            artifact=None,
            metric_nights=recovered,
            cohort=current,
            policy=policy,
        ).current_maturity
        == BaselineMaturity.PROVISIONAL
    )


def test_multiple_metrics_keep_independent_ceiling_and_counts() -> None:
    heart_cohort = cohort("heart_rate_bpm")
    sleep_cohort = cohort("sleep_minutes")
    policy_heart = load_baseline_policy(
        "heart_rate_bpm",
        environment="test",
    )
    policy_sleep = load_baseline_policy(
        "sleep_minutes",
        environment="test",
    )
    heart_count = count(4, measurement_cohort=heart_cohort)
    sleep_count = count(
        1,
        measurement_cohort=sleep_cohort,
        metric_id="sleep_minutes",
    )
    heart = evaluate_readiness(
        decision_id="heart",
        requirement=resolve_claim_requirement(
            "short_window_pattern:heart_rate_bpm"
        ),
        scope_nights=heart_count,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=heart_count,
            cohort=heart_cohort,
            policy=policy_heart,
        ),
        policy=policy_heart,
        capability_receipts=(capability(),),
    )
    sleep = evaluate_readiness(
        decision_id="sleep",
        requirement=resolve_claim_requirement(
            "short_window_pattern:sleep_minutes"
        ),
        scope_nights=sleep_count,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=sleep_count,
            cohort=sleep_cohort,
            policy=policy_sleep,
        ),
        policy=policy_sleep,
        capability_receipts=(capability("adapter.sleep_report"),),
    )
    assert heart.scope_valid_night_count == 4
    assert heart.claim_ceiling == ClaimCeiling.PROVISIONAL_PATTERN
    assert sleep.scope_valid_night_count == 1
    assert sleep.claim_ceiling == ClaimCeiling.SINGLE_NIGHT_DESCRIPTION


def test_established_artifact_supports_one_night_comparison_not_trend() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    baseline_nights = count(15, measurement_cohort=current)
    artifact = ObjectiveBaselineArtifact(
        artifact_id="baseline-1",
        subject_id="subject-1",
        metric_id="heart_rate_bpm",
        window_start=NOW.date() - timedelta(days=14),
        window_end=NOW.date(),
        timezone_name="Asia/Shanghai",
        valid_night_count=15,
        coverage_ratio=0.9,
        quality_status="usable",
        algorithm_id="mean",
        algorithm_version="1.0.0",
        data_version="canonical.v1",
        measurement_cohort_ref=current.cohort_ref,
        policy_version=FIXTURE_BASELINE_POLICY_VERSION,
        source_hash=baseline_nights.source_hash,
        maturity=BaselineMaturity.ESTABLISHED,
        value=62.0,
        source_refs=baseline_nights.source_refs,
        generated_at=NOW,
    )
    projection = project_baseline_readiness(
        artifact=artifact,
        metric_nights=baseline_nights,
        cohort=current,
        policy=policy,
    )
    current_night = count(1, measurement_cohort=current)
    comparison = evaluate_readiness(
        decision_id="compare-baseline",
        requirement=resolve_claim_requirement(
            "current_night_vs_established_baseline:heart_rate_bpm"
        ),
        scope_nights=current_night,
        baseline=projection,
        policy=policy,
        capability_receipts=(capability(),),
    )
    trend = evaluate_readiness(
        decision_id="trend",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=current_night,
        baseline=projection,
        policy=policy,
        capability_receipts=(capability(),),
    )
    assert comparison.response_mode == ResponseMode.SUPPORTED
    assert comparison.claim_ceiling == ClaimCeiling.ESTABLISHED_BASELINE
    assert trend.response_mode == ResponseMode.DEGRADED
    assert trend.claim_ceiling == ClaimCeiling.SINGLE_NIGHT_DESCRIPTION
    assert comparison.scope_valid_night_count == 1
    assert comparison.baseline_valid_night_count == 15


def test_established_history_downgrades_and_recovers_without_rewrite() -> None:
    old_cohort = cohort(adapter_version="1.0.0")
    new_cohort = cohort(adapter_version="2.0.0")
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    old_nights = count(15, measurement_cohort=old_cohort)
    artifact = ObjectiveBaselineArtifact(
        artifact_id="baseline-old-device",
        subject_id="subject-1",
        metric_id="heart_rate_bpm",
        window_start=NOW.date() - timedelta(days=14),
        window_end=NOW.date(),
        timezone_name="Asia/Shanghai",
        valid_night_count=15,
        coverage_ratio=0.9,
        quality_status="usable",
        algorithm_id="mean",
        algorithm_version="1.0.0",
        data_version="canonical.v1",
        measurement_cohort_ref=old_cohort.cohort_ref,
        policy_version=FIXTURE_BASELINE_POLICY_VERSION,
        source_hash=old_nights.source_hash,
        maturity=BaselineMaturity.ESTABLISHED,
        value=62.0,
        source_refs=old_nights.source_refs,
        generated_at=NOW,
    )
    new_nights = count(15, measurement_cohort=new_cohort)
    downgraded = project_baseline_readiness(
        artifact=artifact,
        metric_nights=new_nights,
        cohort=new_cohort,
        policy=policy,
    )
    assert artifact.measurement_cohort_ref == old_cohort.cohort_ref
    assert downgraded.use_eligible is False
    assert downgraded.current_maturity == BaselineMaturity.UNAVAILABLE
    assert (
        ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT
        in downgraded.reason_codes
    )

    rebuilt = artifact.model_copy(
        update={
            "artifact_id": "baseline-new-device",
            "measurement_cohort_ref": new_cohort.cohort_ref,
            "source_hash": new_nights.source_hash,
            "source_refs": new_nights.source_refs,
        }
    )
    recovered = project_baseline_readiness(
        artifact=rebuilt,
        metric_nights=new_nights,
        cohort=new_cohort,
        policy=policy,
    )
    assert recovered.use_eligible is True
    assert recovered.current_maturity == BaselineMaturity.ESTABLISHED


def _snapshot(decision: MetricReadinessDecision) -> FactSnapshot:
    bindings = snapshot_binding_material(
        decisions=(decision,),
        capability_receipts=(capability(),),
    )
    return FactSnapshot.create(
        fact_snapshot_id="snapshot-cold-start",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            authorization_scope=("read_sleep_data",),
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=NOW.date(),
            date_end=NOW.date(),
            valid_night_count=1,
        ),
        canonical_data_version="canonical.v1",
        source_refs=decision.scope_source_refs,
        **bindings,
        created_at=NOW,
    )


def _envelope(snapshot: FactSnapshot, packet: EvidencePacket) -> AgentEnvelope:
    return AgentEnvelope(
        episode_id="episode-1",
        invocation_id="invoke-evidence-1",
        fact_snapshot_id=snapshot.fact_snapshot_id,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        episode_state_revision=1,
        source_scope=snapshot.source_scope,
        target_type="evidence_reasoning",
        target_id="evidence-1",
        target_hash="e" * 64,
        agent_id=AgentId.EVIDENCE_REASONING,
        agent_version="v1",
        skill_id="interpret_scoped_evidence",
        skill_version="v1",
        schema_version="v1",
        policy_version="product-safety.v3",
        status=WorkProductStatus.COMPLETED,
        summary="done",
        output_payload=packet,
    )


def test_evidence_gate_rejects_forged_or_over_ceiling_personal_claim() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(1, measurement_cohort=current)
    decision = evaluate_readiness(
        decision_id="single-night",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=scoped,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=scoped,
            cohort=current,
            policy=policy,
        ),
        policy=policy,
        capability_receipts=(capability(),),
    )
    snapshot = _snapshot(decision)
    cold_receipt = build_cold_start_receipt(
        snapshot=snapshot,
        decisions=(decision,),
        capability_receipts=(capability(),),
        observed_at=NOW,
    )

    def packet(strength: str, metric_id: str = "heart_rate_bpm") -> EvidencePacket:
        return EvidencePacket(
            packet_id="packet-1",
            source_scope=snapshot.source_scope,
            claims=[
                EvidenceClaim(
                    claim_id="claim-1",
                    semantic=EvidenceSemantic.OBSERVED_FACT,
                    statement="这是个人记录结论",
                    source_kind=EvidenceSourceKind.CANONICAL_OBSERVATION,
                    evidence_refs=[scoped.source_refs[0]],
                    confidence=0.8,
                    date_start=NOW.date(),
                    date_end=NOW.date(),
                    claim_strength=strength,
                    metric_id=metric_id,
                    measurement_cohort_ref=current.cohort_ref,
                    readiness_decision_ref=decision.decision_ref,
                )
            ],
        )

    with pytest.raises(AcceptanceError, match="exceeds"):
        accept_evidence(
            _envelope(snapshot, packet("established_baseline")),
            snapshot=snapshot,
            tool_receipts=[cold_receipt],
        )
    with pytest.raises(AcceptanceError, match="metric"):
        accept_evidence(
            _envelope(
                snapshot,
                packet("single_night_description", "breath_rate_bpm"),
            ),
            snapshot=snapshot,
            tool_receipts=[cold_receipt],
        )
    accepted = accept_evidence(
        _envelope(snapshot, packet("single_night_description")),
        snapshot=snapshot,
        tool_receipts=[cold_receipt],
    )
    assert (
        accepted.payload["claims"][0]["readiness_decision_ref"]
        == decision.decision_ref
    )


def test_fact_snapshot_and_publication_fail_closed_on_cold_start_binding() -> None:
    with pytest.raises(ValueError, match="ref/hash mismatch"):
        FactSnapshot.create(
            fact_snapshot_id="forged",
            binding=AuthenticatedBinding(
                actor_id="actor-1",
                subject_id="subject-1",
                role="elder",
            ),
            source_scope=SourceScope(
                kind=SourceScopeKind.CURRENT_NIGHT,
                as_of=NOW,
                timezone_name="Asia/Shanghai",
                date_start=NOW.date(),
                date_end=NOW.date(),
            ),
            canonical_data_version="canonical.v1",
            readiness_decision_refs=(
                f"cold-start-decision:forged:{'a' * 64}",
            ),
            readiness_decision_hashes=("b" * 64,),
            created_at=NOW,
        )

    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(1, measurement_cohort=current)
    decision = evaluate_readiness(
        decision_id="publication-boundary",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=scoped,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=scoped,
            cohort=current,
            policy=policy,
        ),
        policy=policy,
        capability_receipts=(capability(),),
    )
    with pytest.raises(PublicationError, match="reviewed boundary"):
        publication_postflight(
            CommunicationDraft(
                draft_id="draft-1",
                audience_role="elder",
                text="这是模型自由生成的趋势结论。",
                context_notice="仅供测试。",
            ),
            accepted_evidence=None,
            accepted_care=None,
            safety=None,
            safety_required=False,
            readiness_decisions=(decision,),
        )
    boundary = degraded_boundary_sentence(decision)
    publication_postflight(
        CommunicationDraft(
            draft_id="draft-2",
            audience_role="elder",
            text=boundary,
            context_notice="仅供测试。",
        ),
        accepted_evidence=None,
        accepted_care=None,
        safety=None,
        safety_required=False,
        readiness_decisions=(decision,),
    )


def test_caller_cannot_tamper_with_metric_count_or_baseline_projection() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(1, measurement_cohort=current)
    projection = project_baseline_readiness(
        artifact=None,
        metric_nights=scoped,
        cohort=current,
        policy=policy,
    )
    requirement = resolve_claim_requirement(
        "longitudinal_trend:heart_rate_bpm"
    )

    forged_count = scoped.model_copy(
        update={
            "valid_nights": (
                *scoped.valid_nights,
                scoped.valid_nights[0],
            )
        }
    )
    with pytest.raises(ValueError, match="duplicate sleep day"):
        evaluate_readiness(
            decision_id="forged-count",
            requirement=requirement,
            scope_nights=forged_count,
            baseline=projection,
            policy=policy,
            capability_receipts=(capability(),),
        )

    forged_projection = projection.model_copy(
        update={
            "current_maturity": BaselineMaturity.ESTABLISHED,
            "use_eligible": True,
        }
    )
    with pytest.raises(ValueError, match="eligible baseline"):
        evaluate_readiness(
            decision_id="forged-baseline",
            requirement=requirement,
            scope_nights=scoped,
            baseline=forged_projection,
            policy=policy,
            capability_receipts=(capability(),),
        )


def test_degraded_copy_shows_evidence_boundary_without_internal_enums() -> None:
    current = cohort()
    policy = load_baseline_policy("heart_rate_bpm", environment="test")
    scoped = count(3, measurement_cohort=current)
    decision = evaluate_readiness(
        decision_id="three-nights",
        requirement=resolve_claim_requirement(
            "longitudinal_trend:heart_rate_bpm"
        ),
        scope_nights=scoped,
        baseline=project_baseline_readiness(
            artifact=None,
            metric_nights=scoped,
            cohort=current,
            policy=policy,
        ),
        policy=policy,
        capability_receipts=(capability(),),
    )
    text = degraded_boundary_sentence(decision)
    assert "3 晚" in text
    assert "稳定规律" in text
    assert "provisional" not in text


def test_legacy_entry_is_bound_fail_closed_without_synthetic_cohort() -> None:
    decisions = build_unavailable_entry_decisions(
        decision_namespace="legacy-entry",
        claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
    )

    assert {item.metric_id for item in decisions} == {
        "sleep_minutes",
        "in_bed_minutes",
        "out_of_bed_count",
        "movement_count",
        "breath_rate_bpm",
        "heart_rate_bpm",
        "data_coverage_ratio",
    }
    assert all(item.measurement_cohort_ref is None for item in decisions)
    assert all(item.scope_valid_night_count == 0 for item in decisions)
    assert all(item.claim_ceiling == ClaimCeiling.GENERAL_KNOWLEDGE for item in decisions)
    assert all(item.response_mode == ResponseMode.DEGRADED for item in decisions)


def test_missing_exact_cohort_cannot_authorize_a_personal_ceiling() -> None:
    with pytest.raises(ValueError, match="without an exact cohort"):
        MetricReadinessDecision.create(
            decision_id="forged-entry",
            requirement_id="describe_current_night:heart_rate_bpm",
            claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
            metric_id="heart_rate_bpm",
            scope_valid_night_count=1,
            scope_source_hash="a" * 64,
            baseline_source_hash="b" * 64,
            claim_ceiling=ClaimCeiling.SINGLE_NIGHT_DESCRIPTION,
            response_mode=ResponseMode.SUPPORTED,
            reason_codes=(),
        )
