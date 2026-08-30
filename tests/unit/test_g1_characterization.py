from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from sleepagent.domain.contracts import (
    DataMode,
    MovementPayload,
    ObservationType,
    ProviderDeviceIdentity,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.domain.ontology import validate_observation_ontology
from sleepagent.domain.postgres_slice import ReplayObservationInput
from sleepagent.domain.product_data import ProductRevisionFacts
from sleepagent.integrations.perceptor.pull import (
    normalize_history,
    normalize_sleep_report,
)
from sleepagent.integrations.perceptor.push import (
    normalize_push_envelope,
    parse_push_envelope,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.factory import build_deterministic_product_runtime_bundle
from sleepagent.runtime.invocation import EvidenceReasoningModelOutput
from sleepagent.runtime.reports import ReportRole, build_shared_role_projections
from tests.unit.test_product_agent_runner import _shared_analysis_request


pytestmark = pytest.mark.unit
UTC = timezone.utc
FIXTURES = Path(__file__).parents[1] / "fixtures"
G1_FIXTURE = FIXTURES / "remediation" / "g1_characterization.json"
PUSH_FIXTURE = FIXTURES / "perceptor_v2_5_2" / "vital_signs_data_event.json"
REQUESTED = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
RECEIVED = REQUESTED + timedelta(seconds=1)
DEVICE = ProviderDeviceIdentity(
    provider_device_id="synthetic-device-id",
    provider_device_name="SYNTHETIC-DEVICE",
)


def _fixture() -> dict[str, object]:
    return json.loads(G1_FIXTURE.read_text(encoding="utf-8"))


def _product_facts(
    observations: tuple[dict[str, object], ...],
) -> ProductRevisionFacts:
    return ProductRevisionFacts(
        night_episode_id="night:g1-characterization",
        night_episode_revision_id="revision:g1-characterization",
        night_episode_revision_number=1,
        subject_id="subject:g1-characterization",
        data_mode=DataMode.LIVE,
        timezone_name="Asia/Shanghai",
        local_sleep_date="2026-07-10",
        data_sufficiency="sufficient",
        canonical_observations=observations,
        deterministic_quality={
            "schema_version": "deterministic_quality_assessment.v1",
            "quality_state": "good",
            "data_sufficiency": "sufficient",
            "coverage_ratio": 1.0,
        },
        deterministic_risk={
            "risk_state": "no_reviewed_signal",
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        conflict_summaries=(),
        provenance_references=("night_episode_revision:live:g1",),
        canonical_data_version="a" * 64,
    )


def _pull_context() -> dict[str, object]:
    return {
        "provider_account_id": "g1-characterization-account",
        "provider_device": DEVICE,
        "raw_sha256": "b" * 64,
        "requested_at": REQUESTED,
        "received_at": RECEIVED,
    }


def _movement_push_candidate(value: float):
    payload = json.loads(PUSH_FIXTURE.read_text(encoding="utf-8"))
    payload["data"]["BodyShake"] = value
    envelope = parse_push_envelope(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    candidates = normalize_push_envelope(
        envelope,
        provider_account_id="g1-characterization-account",
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED,
    )
    return next(
        item
        for item in candidates
        if item.observation_type is ObservationType.MOVEMENT
    )


def _movement_pull_candidate(value: float):
    result = normalize_history(
        [
            {
                "device_id": "SYNTHETIC-DEVICE",
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": str(value),
                "send_time": "2026-08-23T16:00:00",
            }
        ],
        binding_timezone_name="Asia/Shanghai",
        **_pull_context(),
    )
    return next(
        item
        for item in result.candidates
        if item.observation_type is ObservationType.MOVEMENT
    )


def test_legacy_characterization_mixes_movement_index_and_report_count() -> None:
    fixture = _fixture()["movement_legacy_characterization"]
    assert isinstance(fixture, dict)
    history = _movement_pull_candidate(float(fixture["history_body_shake"]))
    report = normalize_sleep_report(
        {
            "body_shake_data": [
                {
                    "hour": fixture["sleep_report_hour"],
                    "count": fixture["sleep_report_count"],
                }
            ]
        },
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **_pull_context(),
    ).candidates[0]

    assert isinstance(history.payload, MovementPayload)
    assert isinstance(report.payload, MovementPayload)
    assert history.payload.unit == fixture["current_payload_unit_for_both"]
    assert report.payload.unit == fixture["current_payload_unit_for_both"]
    assert history.source_kind is SourceKind.DEVICE_MEASURED
    assert report.source_kind is SourceKind.VENDOR_DERIVED
    assert "vendor_hourly_movement_count_not_continuous_sample" in (
        report.quality.limitations
    )

    # known_semantic_gap: current aggregation discards source/unit meaning and
    # averages the realtime index with the SleepReport hourly count.
    summary = _product_facts(
        (
            {
                "source_kind": history.source_kind.value,
                "payload": history.payload.model_dump(mode="json"),
            },
            {
                "source_kind": report.source_kind.value,
                "payload": report.payload.model_dump(mode="json"),
            },
        )
    ).deterministic_night_summary()
    assert summary["sample_counts"]["movement"] == 2
    assert summary["vital_centers"]["movement"] == fixture[
        "known_semantic_gap_average"
    ]


def test_push_pull_replay_valid_movement_path_characterization() -> None:
    fixture = _fixture()["path_characterization"]
    assert isinstance(fixture, dict)
    value = float(fixture["value"])

    with patch(
        "sleepagent.domain.ontology.validate_observation_ontology",
        wraps=validate_observation_ontology,
    ) as live_validator:
        push = _movement_push_candidate(value)
        pull = _movement_pull_candidate(value)
    assert live_validator.call_count == 0

    with patch(
        "sleepagent.domain.postgres_slice.validate_observation_ontology",
        wraps=validate_observation_ontology,
    ) as replay_validator:
        replay = ReplayObservationInput(
            provider_id="perceptor",
            provider_account_id="g1-characterization-account",
            provider_device_id="synthetic-device-id",
            subject_id="subject:g1-characterization",
            device_id="device:g1-characterization",
            device_binding_id="binding:g1-characterization",
            binding_version=1,
            timezone_name="Asia/Shanghai",
            observation_type=ObservationType.MOVEMENT,
            payload=MovementPayload(value=value),
            source_kind=SourceKind.DEVICE_MEASURED,
            quality=pull.quality,
            measurement_at=pull.measurement_at,
            received_at=pull.received_at,
            timezone_status=pull.timezone_status,
            source_key="source:g1-valid-movement",
            idempotency_identity="replay:g1-valid-movement",
        )
    assert replay_validator.call_count == 1

    expected_payload = fixture["canonical_payload"]
    assert push.payload.model_dump(mode="json") == expected_payload
    assert pull.payload.model_dump(mode="json") == expected_payload
    assert replay.payload.model_dump(mode="json") == expected_payload
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    observed = {
        "push": {
            "accepted": True,
            "ontology_validation_invoked": False,
            "source_kind": push.source_kind.value,
            "provenance_adapter_id": push.provenance.adapter_id,
        },
        "pull": {
            "accepted": True,
            "ontology_validation_invoked": False,
            "source_kind": pull.source_kind.value,
            "provenance_adapter_id": pull.provenance.adapter_id,
        },
        "replay": {
            "accepted": True,
            "ontology_validation_invoked": True,
            "source_kind": replay.source_kind.value,
            "provenance_adapter_id": None,
        },
    }
    assert observed == expected


def test_pull_accepts_vendor_count_that_equivalent_replay_rejects() -> None:
    report = normalize_sleep_report(
        {"body_shake_data": [{"hour": 0, "count": 2}]},
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **_pull_context(),
    ).candidates[0]
    assert report.source_kind is SourceKind.VENDOR_DERIVED

    # known_semantic_gap: live Pull does not invoke ontology validation, while
    # Replay rejects the same v1 payload/source pair.
    with pytest.raises(ValueError, match="movement requires device_measured source"):
        ReplayObservationInput(
            provider_id="perceptor",
            provider_account_id="g1-characterization-account",
            provider_device_id="synthetic-device-id",
            subject_id="subject:g1-characterization",
            device_id="device:g1-characterization",
            device_binding_id="binding:g1-characterization",
            binding_version=1,
            timezone_name="Asia/Shanghai",
            observation_type=ObservationType.MOVEMENT,
            payload=report.payload,
            source_kind=report.source_kind,
            quality=report.quality,
            measurement_at=report.measurement_at,
            received_at=report.received_at,
            timezone_status=report.timezone_status,
            source_key="source:g1-report-count",
            idempotency_identity="replay:g1-report-count",
        )


def test_timezone_legacy_characterization_exposes_utc_as_local_clock_gap() -> None:
    fixture = _fixture()["timezone_legacy_characterization"]
    assert isinstance(fixture, dict)
    bed_exit = datetime.fromisoformat(str(fixture["utc_bed_exit"]))
    start = datetime.fromisoformat(str(fixture["sleep_window_start_utc"]))
    end = datetime.fromisoformat(str(fixture["sleep_window_end_utc"]))
    facts = _product_facts(
        (
            {
                "measurement_at": bed_exit.isoformat(),
                "payload": {"observation_type": "bed_exit", "kind": "bed_exit"},
            },
            {
                "measurement_at": start.isoformat(),
                "payload": {
                    "observation_type": "sleep_stage_interval",
                    "stage": "light",
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                },
            },
        )
    )

    summary = facts.deterministic_night_summary()
    local = bed_exit.astimezone(ZoneInfo(str(fixture["timezone_name"])))
    assert local.date().isoformat() == fixture["correct_local_date"]
    assert local.strftime("%H:%M") == fixture["correct_local_clock"]
    assert summary["sleep_window_minutes"] == 360.0
    assert summary["sleep_window_start"] == start.isoformat()
    assert summary["bed_exit_events"][0]["local_time"] == fixture[
        "current_mislabeled_local_clock"
    ]
    assert summary["bed_exit_events"][0]["local_time"] != fixture[
        "correct_local_clock"
    ]


class _EnglishClaimModel(DeterministicReplayStructuredAgentModel):
    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not EvidenceReasoningModelOutput:
            return result
        fixture = _fixture()["localization_legacy_characterization"]
        assert isinstance(fixture, dict)
        packet = result.output_payload
        claims = [
            claim.model_copy(update={"statement": fixture["english_claim"]})
            for claim in packet.claims
        ]
        return result.model_copy(
            update={"output_payload": packet.model_copy(update={"claims": claims})}
        )


def test_english_evidence_claim_leaks_into_chinese_family_projection() -> None:
    fixture = _fixture()["localization_legacy_characterization"]
    assert isinstance(fixture, dict)
    runner = build_deterministic_product_runtime_bundle(
        model=_EnglishClaimModel()
    ).runner
    shared = runner.analyze_shared(_shared_analysis_request())
    family = next(
        projection
        for projection in build_shared_role_projections(shared)
        if projection.role is ReportRole.FAMILY
    )

    assert family.text is not None
    assert fixture["chinese_family_heading"] in family.text
    assert fixture["english_claim"] in shared.summary_lines
    assert fixture["english_claim"] in family.text
