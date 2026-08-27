from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sleepagent.runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    ContextPacket,
    FactSnapshot,
    InvocationOutcome,
    SourceScope,
    SourceScopeKind,
    TrustLabel,
    TrustedContextItem,
    provider_context_projection,
    stable_hash,
)
from sleepagent.runtime.tooling import (
    CoreProductToolService,
    ProductToolExecutionContext,
    ProductToolExecutor,
)
from sleepagent.domain.contracts import DataMode
from sleepagent.domain.product_data import (
    ProductNightVitalSummary,
    ProductRevisionFacts,
    build_longitudinal_vital_risk_context,
)


NIGHT = date(2026, 7, 9)


def _vital_summary(
    day: int,
    heart_rate: float,
    respiratory_rate: float,
) -> ProductNightVitalSummary:
    return ProductNightVitalSummary(
        local_sleep_date=date(2026, 7, day),
        night_episode_revision_ref=f"night_episode_revision:live:{day}",
        heart_rate_center=heart_rate,
        respiratory_rate_center=respiratory_rate,
        heart_rate_sample_count=120,
        respiratory_rate_sample_count=120,
    )


def test_longitudinal_vital_watch_requires_three_consistent_meaningful_nights() -> None:
    rising = (
        _vital_summary(7, 64.0, 14.0),
        _vital_summary(8, 72.0, 17.0),
        _vital_summary(9, 82.0, 20.0),
    )

    context = build_longitudinal_vital_risk_context(rising)

    assert context is not None
    assert context.reason_codes == ("consistent_vital_increase_three_nights",)
    assert context.trend_signals[0].risk_level == "watch"
    assert context.trend_signals[0].source_refs == tuple(
        item.night_episode_revision_ref for item in rising
    )
    facts = _product_facts().model_copy(
        update={
            "longitudinal_risk_context": context,
            "provenance_references": (
                "night_episode_revision:live:1",
                *context.trend_signals[0].source_refs,
            ),
        }
    )
    risk_arguments = facts.tool_inputs()["risk.classify_signal"]
    assert "trend_signals" not in risk_arguments["data"]
    assert risk_arguments["trend_signals"][0]["risk_level"] == "watch"
    assert risk_arguments["trend_observation"]["quality_status"] == "good"
    assert build_longitudinal_vital_risk_context(rising[:2]) is None
    assert build_longitudinal_vital_risk_context(
        (
            _vital_summary(7, 64.0, 14.0),
            _vital_summary(8, 65.0, 14.2),
            _vital_summary(9, 64.5, 14.1),
        )
    ) is None


def test_product_night_evidence_tool_rejects_cross_subject_canonical_facts() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": _product_facts(subject_id="different-subject").model_dump(
                mode="json"
            ),
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome == InvocationOutcome.FAILED
    assert result.receipt.error_code == "ValueError"


def test_product_night_evidence_accepts_explicit_namespaced_subject_binding() -> None:
    facts = _product_facts()
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": facts.model_dump(mode="json"),
            "source_refs": list(facts.agent_source_refs()),
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(
                binding_subject="tenant-live::subject::elder-phase3a",
                source_refs=facts.agent_source_refs(),
            ),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED


def test_product_night_evidence_accepts_privacy_safe_hashed_subject_binding() -> None:
    subject_id = "elder-phase3a"
    facts = _product_facts(subject_id=subject_id)
    subject_ref = "subject:" + stable_hash(
        {"data_mode": "live", "subject_id": subject_id}
    )[:32]
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": facts.model_dump(mode="json"),
            "source_refs": list(facts.agent_source_refs()),
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(
                binding_subject=subject_ref,
                source_refs=facts.agent_source_refs(),
            ),
            episode_id="episode-phase3a-private-subject",
        ),
    )

    assert subject_id not in subject_ref
    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED


def test_product_night_evidence_projects_large_provenance_to_bounded_summary() -> None:
    subject_id = "elder-phase3a"
    facts = _product_facts(subject_id=subject_id).model_copy(
        update={
            "provenance_references": (
                "night_episode_revision:live:1",
                *tuple(
                    f"canonical_observation:observation-{index}"
                    for index in range(100)
                ),
            )
        }
    )
    tool_input = facts.tool_inputs()["radar.get_night_evidence"]
    subject_ref = "subject:" + stable_hash(
        {"data_mode": "live", "subject_id": subject_id}
    )[:32]
    fact_snapshot = _snapshot(
        binding_subject=subject_ref,
        source_refs=tuple(tool_input["source_refs"]),
    )

    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        tool_input,
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=fact_snapshot,
            episode_id="episode-phase3a-bounded-evidence",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert len(result.receipt.source_refs) <= 50
    assert result.receipt.output["data"]["schema_version"] == (
        "product_night_evidence.v1"
    )
    assert result.receipt.source_refs == list(facts.agent_source_refs())
    assert "subject_id" not in result.receipt.output["data"]
    assert "canonical_observations" not in result.receipt.output["data"]
    assert "provenance_set_sha256" not in result.receipt.output["data"]


def test_provider_projection_growth_is_independent_of_membership_id_volume() -> None:
    membership_ids = tuple(f"private-membership-{index}" for index in range(12_000))
    subject_id = "private-subject-primary-key"
    quality = {
        **_product_facts().deterministic_quality,
        "subject_id": subject_id,
        "night_episode_id": "private-night-primary-key",
        "assessment_id": "private-quality-primary-key",
        "source_scope": {
            "night_episode_id": "private-night-primary-key",
            "night_episode_revision_id": "private-revision-primary-key",
            "observation_ids": membership_ids,
            "observation_types": ["heart_rate", "respiratory_rate"],
            "device_binding_ids": ["private-device-binding-primary-key"],
            "window_start_at": "2026-07-09T22:00:00+08:00",
            "window_end_at": "2026-07-10T06:00:00+08:00",
        },
    }
    facts = _product_facts(subject_id=subject_id).model_copy(
        update={
            "deterministic_quality": quality,
            "deterministic_risk": {
                "risk_state": "unknown",
                "subject_id": subject_id,
                "night_episode_id": "private-night-primary-key",
                "current_risk_id": "private-risk-primary-key",
                "source_scope": quality["source_scope"],
                "reason_codes": ["partial_quality"],
            },
            "provenance_references": membership_ids,
        }
    )

    provider_json = json.dumps(
        facts.tool_inputs(), ensure_ascii=False, sort_keys=True
    )
    one_id_json = json.dumps(
        facts.model_copy(
            update={
                "deterministic_quality": {
                    **quality,
                    "source_scope": {
                        **quality["source_scope"],
                        "observation_ids": membership_ids[:1],
                    },
                },
                "provenance_references": membership_ids[:1],
            }
        ).tool_inputs(),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert len(facts.deterministic_quality["source_scope"]["observation_ids"]) == 12_000
    assert subject_id not in provider_json
    assert membership_ids[0] not in provider_json
    assert membership_ids[-1] not in provider_json
    assert "private-night-primary-key" not in provider_json
    assert "private-device-binding-primary-key" not in provider_json
    assert len(provider_json) - len(one_id_json) < 200


def test_provider_context_projection_strips_local_runtime_and_subject_ids() -> None:
    snapshot = _snapshot()
    context = ContextPacket(
        context_packet_id="private-context-id",
        episode_id="private-product-episode-id",
        invocation_id="private-invocation-id",
        agent_id=AgentId.EVIDENCE_REASONING,
        objective="Explain the governed night evidence.",
        fact_snapshot_id="private-fact-snapshot-id",
        fact_snapshot_hash="b" * 64,
        source_scope=snapshot.source_scope,
        items=(
            TrustedContextItem(
                key="canonical_fact",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={
                    "subject_id": "private-subject-id",
                    "night_episode_id": "private-night-id",
                    "quality_state": "partial",
                    "valid_until": datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
                },
                source_refs=("governed_evidence_set:sha256:" + "c" * 64,),
            ),
        ),
    )

    serialized = json.dumps(provider_context_projection(context), sort_keys=True)

    for private_value in (
        "private-context-id",
        "private-product-episode-id",
        "private-invocation-id",
        "private-fact-snapshot-id",
        "private-subject-id",
        "private-night-id",
    ):
        assert private_value not in serialized
    assert '"quality_state": "partial"' in serialized
    assert '"valid_until": "2026-07-10T08:00:00+00:00"' in serialized


def test_product_night_evidence_includes_deterministic_sleep_and_bed_exit_summary() -> None:
    facts = _product_facts().model_copy(
        update={
            "canonical_observations": (
                {
                    "measurement_at": "2026-07-09T22:30:00+08:00",
                    "payload": {
                        "observation_type": "sleep_stage_interval",
                        "stage": "light",
                        "start_at": "2026-07-09T22:30:00+08:00",
                        "end_at": "2026-07-10T02:00:00+08:00",
                    },
                },
                {
                    "measurement_at": "2026-07-10T02:00:00+08:00",
                    "payload": {
                        "observation_type": "bed_exit",
                        "kind": "bed_exit",
                    },
                },
                {
                    "measurement_at": "2026-07-10T02:05:00+08:00",
                    "payload": {
                        "observation_type": "bed_exit",
                        "kind": "return_to_bed",
                    },
                },
                {
                    "measurement_at": "2026-07-10T02:05:00+08:00",
                    "payload": {
                        "observation_type": "sleep_stage_interval",
                        "stage": "light",
                        "start_at": "2026-07-10T02:05:00+08:00",
                        "end_at": "2026-07-10T06:30:00+08:00",
                    },
                },
                {
                    "measurement_at": "2026-07-10T03:00:00+08:00",
                    "payload": {
                        "observation_type": "heart_rate",
                        "value": 62.0,
                    },
                },
                {
                    "measurement_at": "2026-07-10T03:00:00+08:00",
                    "payload": {
                        "observation_type": "respiratory_rate",
                        "value": 14.0,
                    },
                },
            )
        }
    )

    summary = facts.agent_night_evidence()["deterministic_night_summary"]

    assert summary["sleep_window_minutes"] == 480.0
    assert summary["stage_minutes"] == {"light": 475.0}
    assert summary["vital_centers"]["heart_rate"] == 62.0
    assert summary["vital_centers"]["respiratory_rate"] == 14.0
    assert summary["bed_exit_count"] == 1
    assert summary["bed_exit_events"] == [
        {
            "left_bed_at": "2026-07-10T02:00:00+08:00",
            "local_time": "02:00",
            "returned_at": "2026-07-10T02:05:00+08:00",
            "duration_minutes": 5.0,
        }
    ]


def test_agent_cannot_promote_caller_supplied_generic_radar_payload() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": {"total_sleep_minutes": 999},
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_agent_cannot_submit_even_well_formed_canonical_radar_facts() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": _product_facts().model_dump(mode="json"),
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_runtime_canonical_radar_facts_require_complete_typed_payload() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_night_evidence",
        {
            "data": {
                "schema_version": "product_revision_facts.v1",
                "subject_id": "elder-phase3a",
                "canonical_data_version": "a" * 64,
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_quality_tool_preserves_pinned_fail_closed_assessment() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.assess_data_quality",
        {
            "coverage_ratio": 0.99,
            "data": {
                "schema_version": "deterministic_quality_assessment.v1",
                "policy_version": "quality-policy-reviewed.v3",
                "quality_state": "data_insufficient",
                "data_sufficiency": "data_insufficient",
                "stale": False,
                "offline": True,
                "clock_invalid": False,
                "reason_codes": ["device_offline"],
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome == InvocationOutcome.SUCCEEDED
    assert result.receipt.output["usable"] is False
    assert result.receipt.output["policy_version"] == (
        "quality-policy-reviewed.v3"
    )
    assert result.receipt.output["reason_codes"] == ["device_offline"]


def test_product_quality_tool_treats_partial_as_usable_with_limitations() -> None:
    facts = _product_facts().model_copy(
        update={
            "data_sufficiency": "partial",
            "deterministic_quality": {
                "schema_version": "deterministic_quality_assessment.v1",
                "policy_version": "quality-v2-semantic-missingness",
                "quality_state": "partial",
                "data_sufficiency": "partial",
                "coverage_ratio": 1.0,
                "explicit_missing_interval_count": 15,
                "invalid_observation_count": 15,
                "stale": False,
                "offline": False,
                "clock_invalid": False,
                "reason_codes": ["explicit_missing_observations"],
            },
        }
    )
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.assess_data_quality",
        facts.tool_inputs()["radar.assess_data_quality"],
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(source_refs=facts.agent_source_refs()),
            episode_id="episode-partial-quality",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["usable"] is True
    assert result.receipt.output["data_sufficiency"] == "partial"
    assert result.receipt.output["reason_codes"] == [
        "explicit_missing_observations"
    ]


def test_agent_cannot_self_attest_pinned_quality_policy() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.assess_data_quality",
        {
            "data": {
                "schema_version": "deterministic_quality_assessment.v1",
                "policy_version": "caller-invented.v1",
                "quality_state": "good",
                "data_sufficiency": "sufficient",
                "coverage_ratio": 1.0,
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_quality_tool_rejects_source_outside_fact_snapshot() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.assess_data_quality",
        {
            "coverage_ratio": 0.95,
            "source_refs": ["quality:not-authorized"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_device_status_tool_uses_typed_canonical_projection() -> None:
    facts = _product_facts()
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_device_status",
        facts.tool_inputs()["radar.get_device_status"],
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(source_refs=facts.agent_source_refs()),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output == {
        "schema_version": "canonical_radar_device_status.v1",
        "data_mode": "live",
        "offline": False,
        "stale": False,
        "source_refs": list(facts.agent_source_refs()),
    }


def test_product_device_status_rejects_unbound_source() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "radar.get_device_status",
        {
            "data": {
                "schema_version": "canonical_radar_device_status.v1",
                "data_mode": "live",
                "offline": False,
                "stale": False,
            },
            "source_refs": ["device-status:not-authorized"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_agent_night_evidence_preserves_vendor_and_pull_authority() -> None:
    facts = _product_facts().model_copy(
        update={
            "data_sufficiency": "partial",
            "canonical_observations": (
                {
                    "source_kind": "vendor_derived",
                    "acquisition_channels": ["PULL"],
                    "payload": {
                        "observation_type": "sleep_stage_interval",
                        "stage": "deep",
                        "start_at": "2026-07-09T22:00:00+08:00",
                        "end_at": "2026-07-09T22:30:00+08:00",
                    },
                },
                {
                    "source_kind": "device_measured",
                    "acquisition_channels": ["PULL"],
                    "payload": {
                        "observation_type": "heart_rate",
                        "value": 60,
                    },
                },
                {
                    "source_kind": "device_measured",
                    "acquisition_channels": ["PUSH"],
                    "payload": {
                        "observation_type": "respiratory_rate",
                        "value": 14,
                    },
                },
            ),
        }
    )

    evidence = facts.agent_night_evidence()
    authority = evidence["evidence_authority"]

    assert authority["sleep_stage_authority"] == "vendor_derived"
    assert authority["independent_sleepagent_stage_classification"] is False
    assert authority["pull_backfilled_measurement_count"] == 1
    assert authority["matched_push_pull_measurement_count"] == 0
    assert authority["push_pull_relation"] == "non_identical_cadence"
    assert authority["pull_timestamp_semantics"] == (
        "reconstructed_from_vendor_batch_cadence"
    )
    assert "subject_id" not in evidence
    assert "device_ref" not in evidence


def _snapshot(
    *,
    binding_subject: str = "elder-phase3a",
    source_refs: tuple[str, ...] | None = None,
) -> FactSnapshot:
    as_of = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    return FactSnapshot.create(
        fact_snapshot_id="phase3a-radar-snapshot",
        binding=AuthenticatedBinding(
            actor_id="elder-phase3a",
            subject_id=binding_subject,
            role="elder",
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=as_of,
            timezone_name="Asia/Shanghai",
            date_start=NIGHT,
            date_end=NIGHT,
            valid_night_count=1,
        ),
        canonical_data_version="a" * 64,
        source_refs=source_refs or ("night_episode_revision:live:1",),
        created_at=as_of,
    )


def _product_facts(
    *,
    subject_id: str = "elder-phase3a",
) -> ProductRevisionFacts:
    return ProductRevisionFacts(
        night_episode_id="night-phase3a",
        night_episode_revision_id="revision-phase3a",
        night_episode_revision_number=1,
        subject_id=subject_id,
        data_mode=DataMode.LIVE,
        timezone_name="Asia/Shanghai",
        local_sleep_date=NIGHT.isoformat(),
        data_sufficiency="sufficient",
        canonical_observations=(),
        deterministic_quality={
            "schema_version": "deterministic_quality_assessment.v1",
            "policy_version": "quality-policy-reviewed.v3",
            "coverage_ratio": 0.9,
            "data_sufficiency": "sufficient",
            "quality_state": "good",
        },
        deterministic_risk={
            "risk_state": "no_reviewed_signal",
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        conflict_summaries=(),
        provenance_references=("night_episode_revision:live:1",),
        canonical_data_version="a" * 64,
    )
