from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from sleepagent.config import ObservationSemanticsVersion, SleepBackendSettings
from sleepagent.domain.canonical_observation import CanonicalObservationFactoryV2
from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AvailabilityState,
    CalibrationValue,
    ConfidenceValue,
    DataMode,
    MissingState,
    MovementPayload,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.domain.observation_semantics import (
    CanonicalObservationRejected,
    MovementMetricId,
    MovementPayloadV2,
    SemanticRejectionCategory,
    movement_metric_definition,
    validate_movement_semantics_v2,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    ReplayObservationInputV2,
    canonicalize_replay_input_v2,
)
from sleepagent.integrations.perceptor.pull import (
    canonicalize_pull_result_v2,
    normalize_current,
    normalize_history,
    normalize_realtime,
    normalize_sleep_report,
)
from sleepagent.integrations.perceptor.push import (
    canonicalize_push_candidates_v2,
    normalize_push_envelope,
    parse_push_envelope,
)
from sleepagent.simulation import CanonicalReplayGenerator, load_packaged_scenario
from sleepagent.simulation.replay_ingress import ReplayExternalFactAdapter


pytestmark = pytest.mark.unit
UTC = timezone.utc
AT = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
RECEIVED = AT + timedelta(seconds=1)
DEVICE = ProviderDeviceIdentity(provider_device_id="semantic-device")
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "perceptor_v2_5_2"
    / "vital_signs_data_event.json"
)


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
    )


def _push_movement(value: float = 12.5):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["data"]["BodyShake"] = value
    envelope = parse_push_envelope(json.dumps(payload).encode("utf-8"))
    candidates = normalize_push_envelope(
        envelope,
        provider_account_id="semantic-account",
        data_mode=DataMode.REPLAY,
        received_at=RECEIVED,
    )
    candidate = next(
        item for item in candidates if item.observation_type is ObservationType.MOVEMENT
    )
    return candidate.model_copy(
        update={
            "provider_device": DEVICE,
            "measurement_at": AT,
            "received_at": RECEIVED,
        }
    )


def _pull_index(value: float = 12.5):
    result = normalize_history(
        [
            {
                "device_id": "semantic-device",
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": str(value),
                "send_time": "2026-08-23T16:00:00",
            }
        ],
        provider_account_id="semantic-account",
        provider_device=DEVICE,
        raw_sha256="b" * 64,
        requested_at=AT,
        received_at=RECEIVED,
        binding_timezone_name="Asia/Shanghai",
    )
    candidate = next(
        item
        for item in result.candidates
        if item.observation_type is ObservationType.MOVEMENT
    )
    return result, candidate.model_copy(update={"measurement_at": AT})


def _pull_count(value: int | float = 12):
    return normalize_sleep_report(
        {"body_shake_data": [{"hour": 16, "count": value}]},
        provider_account_id="semantic-account",
        provider_device=DEVICE,
        raw_sha256="c" * 64,
        requested_at=AT,
        received_at=RECEIVED,
        report_date=date(2026, 8, 23),
        binding_timezone_name="UTC",
    )


def _replay_movement(
    semantic_payload: MovementPayloadV2,
    *,
    source_kind: SourceKind,
    legacy_unit: str,
) -> ReplayObservationInputV2:
    return ReplayObservationInputV2(
        provider_id="perceptor",
        provider_account_id="semantic-account",
        provider_device_id="semantic-device",
        subject_id="subject:semantic",
        device_id="device:semantic",
        device_binding_id="binding:semantic",
        binding_version=1,
        timezone_name="UTC",
        observation_type=ObservationType.MOVEMENT,
        payload=MovementPayload(value=semantic_payload.value, unit=legacy_unit),
        movement_payload_v2=semantic_payload,
        source_kind=source_kind,
        quality=_quality(),
        measurement_at=AT,
        received_at=RECEIVED,
        timezone_status=TimezoneStatus.KNOWN,
        source_key="source:semantic",
        idempotency_identity="replay:semantic",
    )


def _projection(value):
    return {
        "metric_id": value.metric_id,
        "value": value.payload.value,
        "unit": value.canonical_unit,
        "occurred_at": value.occurred_at,
        "aggregation_start_at": value.aggregation_start_at,
        "aggregation_end_at": value.aggregation_end_at,
        "source_kind": value.source_kind,
        "trusted_for_analytics": value.trusted_for_analytics,
    }


def test_movement_v2_registry_contracts_are_semantically_distinct() -> None:
    index = movement_metric_definition(MovementMetricId.MOVEMENT_INDEX)
    count = movement_metric_definition(MovementMetricId.MOVEMENT_EVENT_COUNT)
    ambiguous = movement_metric_definition(
        MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT
    )

    assert index.allowed_units == frozenset({"vendor_index"})
    assert not index.aggregation_window_required
    assert count.allowed_units == frozenset({"count"})
    assert count.aggregation_window_required
    assert index.metric_id != count.metric_id
    assert not ambiguous.trusted_for_analytics


def test_movement_index_accepts_floating_point_vendor_index() -> None:
    payload = MovementPayloadV2(
        metric_id=MovementMetricId.MOVEMENT_INDEX,
        value=12.5,
        unit="vendor_index",
        vendor_semantic_code="BodyShake",
    )
    definition = validate_movement_semantics_v2(
        payload, source_kind=SourceKind.DEVICE_MEASURED
    )
    assert definition.trusted_for_analytics


def test_movement_event_count_accepts_integer_with_window() -> None:
    payload = MovementPayloadV2(
        metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
        value=12,
        unit="count",
        aggregation_start_at=AT,
        aggregation_end_at=AT + timedelta(hours=1),
    )
    assert validate_movement_semantics_v2(
        payload, source_kind=SourceKind.VENDOR_DERIVED
    ).metric_id is MovementMetricId.MOVEMENT_EVENT_COUNT


def test_negative_movement_value_is_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_INDEX,
            value=-1,
            unit="vendor_index",
        )


@pytest.mark.parametrize(
    ("payload", "source_kind", "category"),
    [
        (
            MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
                value=1.5,
                unit="count",
                aggregation_start_at=AT,
                aggregation_end_at=AT + timedelta(hours=1),
            ),
            SourceKind.VENDOR_DERIVED,
            SemanticRejectionCategory.INVALID_VALUE,
        ),
        (
            MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
                value=1,
                unit="count",
            ),
            SourceKind.VENDOR_DERIVED,
            SemanticRejectionCategory.MISSING_AGGREGATION_WINDOW,
        ),
        (
            MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
                value=1,
                unit="count",
                aggregation_start_at=AT,
                aggregation_end_at=AT,
            ),
            SourceKind.VENDOR_DERIVED,
            SemanticRejectionCategory.INVALID_AGGREGATION_WINDOW,
        ),
        (
            MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_INDEX,
                value=1,
                unit="count",
            ),
            SourceKind.DEVICE_MEASURED,
            SemanticRejectionCategory.INVALID_UNIT,
        ),
        (
            MovementPayloadV2(
                metric_id=MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT,
                value=1,
                unit="legacy_unknown",
            ),
            SourceKind.VENDOR_DERIVED,
            SemanticRejectionCategory.UNSUPPORTED_VENDOR_SEMANTICS,
        ),
    ],
)
def test_movement_contract_rejection_categories(payload, source_kind, category) -> None:
    with pytest.raises(CanonicalObservationRejected) as rejected:
        validate_movement_semantics_v2(payload, source_kind=source_kind)
    assert rejected.value.category is category


def test_factory_normalizes_authoritative_time_and_separates_receipt_identity() -> None:
    candidate = _push_movement().model_copy(
        update={"measurement_at": datetime(2026, 8, 23, 16, tzinfo=timezone(timedelta(hours=8)))}
    )
    canonical = CanonicalObservationFactoryV2().build(
        candidate=candidate,
        movement_payload=MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_INDEX,
            value=12.5,
            unit="vendor_index",
            vendor_semantic_code="BodyShake",
        ),
        normalizer_version="test-normalizer.v2",
    )
    assert canonical.occurred_at == AT
    assert canonical.transport_receipt_identity == candidate.idempotency_key
    assert canonical.semantic_identity != candidate.idempotency_key


def test_factory_rejects_invalid_schema_provenance_timestamp_and_semantics() -> None:
    factory = CanonicalObservationFactoryV2()
    with pytest.raises(CanonicalObservationRejected) as schema:
        factory.build(candidate={}, normalizer_version="test.v2")
    assert schema.value.category is SemanticRejectionCategory.INVALID_SCHEMA

    candidate = _push_movement()
    mapping = candidate.model_dump(mode="python")
    mapping["provenance"]["provider_account_id"] = "other-account"
    with pytest.raises(CanonicalObservationRejected) as provenance:
        factory.build(candidate=mapping, normalizer_version="test.v2")
    assert (
        provenance.value.category
        is SemanticRejectionCategory.INVALID_SOURCE_PROVENANCE
    )

    mapping = candidate.model_dump(mode="python")
    mapping["measurement_at"] = AT.replace(tzinfo=None)
    with pytest.raises(CanonicalObservationRejected) as timestamp:
        factory.build(candidate=mapping, normalizer_version="test.v2")
    assert timestamp.value.category is SemanticRejectionCategory.INVALID_TIMESTAMP

    with pytest.raises(CanonicalObservationRejected) as semantic:
        factory.build(
            candidate=candidate,
            movement_payload=MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_INDEX,
                value=12.5,
                unit="count",
            ),
            normalizer_version="test.v2",
        )
    assert semantic.value.category is SemanticRejectionCategory.INVALID_UNIT


def test_push_pull_replay_v2_valid_index_semantic_projection_matches() -> None:
    push = canonicalize_push_candidates_v2((_push_movement(),))[0]
    pull_result, pull_candidate = _pull_index()
    pull_result = type(pull_result)(candidates=(pull_candidate,))
    pull = canonicalize_pull_result_v2(pull_result)[0]
    replay = canonicalize_replay_input_v2(
        _replay_movement(
            MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_INDEX,
                value=12.5,
                unit="vendor_index",
                vendor_semantic_code="perceptor.body_shake.index",
            ),
            source_kind=SourceKind.DEVICE_MEASURED,
            legacy_unit="index",
        )
    )

    assert _projection(push) == _projection(pull) == _projection(replay)
    assert push.semantic_identity == pull.semantic_identity == replay.semantic_identity


def test_pull_replay_v2_valid_event_count_and_provenance_parity() -> None:
    pull = canonicalize_pull_result_v2(_pull_count())[0]
    replay_input = _replay_movement(
        MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
            value=12,
            unit="count",
            aggregation_start_at=AT + timedelta(hours=8),
            aggregation_end_at=AT + timedelta(hours=9),
            vendor_semantic_code="perceptor.body_shake.hourly_count",
        ),
        source_kind=SourceKind.VENDOR_DERIVED,
        legacy_unit="count",
    ).model_copy(update={"measurement_at": AT + timedelta(hours=8)})
    replay = canonicalize_replay_input_v2(replay_input)
    assert _projection(pull) == _projection(replay)
    assert pull.semantic_identity == replay.semantic_identity
    assert pull.provenance.adapter_id != replay.provenance.adapter_id


@pytest.mark.parametrize("transport", ["push", "pull", "replay"])
def test_v2_missing_count_window_rejects_consistently_after_transport_mapping(
    transport: str,
) -> None:
    candidate = _push_movement().model_copy(
        update={
            "source_kind": SourceKind.VENDOR_DERIVED,
            "idempotency_key": f"{transport}:missing-window",
        }
    )
    with pytest.raises(CanonicalObservationRejected) as rejected:
        CanonicalObservationFactoryV2().build(
            candidate=candidate,
            movement_payload=MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
                value=12,
                unit="count",
                vendor_semantic_code=f"{transport}.decoded_count",
            ),
            normalizer_version=f"{transport}.v2",
        )
    assert (
        rejected.value.category
        is SemanticRejectionCategory.MISSING_AGGREGATION_WINDOW
    )


@pytest.mark.parametrize("transport", ["push", "pull", "replay"])
def test_v2_metric_unit_mismatch_rejects_consistently(transport: str) -> None:
    candidate = _push_movement().model_copy(
        update={"idempotency_key": f"{transport}:invalid-unit"}
    )
    with pytest.raises(CanonicalObservationRejected) as rejected:
        CanonicalObservationFactoryV2().build(
            candidate=candidate,
            movement_payload=MovementPayloadV2(
                metric_id=MovementMetricId.MOVEMENT_INDEX,
                value=12.5,
                unit="count",
            ),
            normalizer_version=f"{transport}.v2",
        )
    assert rejected.value.category is SemanticRejectionCategory.INVALID_UNIT


def test_unproved_pull_vendor_series_semantics_fail_closed() -> None:
    result = normalize_sleep_report(
        {"body_shake_data": [{"time_long": int(AT.timestamp()), "value": 4}]},
        provider_account_id="semantic-account",
        provider_device=DEVICE,
        raw_sha256="d" * 64,
        requested_at=AT,
        received_at=RECEIVED,
        report_date=date(2026, 8, 23),
        binding_timezone_name="UTC",
    )
    with pytest.raises(CanonicalObservationRejected) as rejected:
        canonicalize_pull_result_v2(result)
    assert (
        rejected.value.category
        is SemanticRejectionCategory.UNSUPPORTED_VENDOR_SEMANTICS
    )


def test_feature_default_is_v2_and_explicit_v1_preserves_legacy_payload() -> None:
    assert (
        SleepBackendSettings.model_fields["observation_semantics_version"].default
        is ObservationSemanticsVersion.V2
    )
    candidate = _push_movement()
    assert isinstance(candidate.payload, MovementPayload)
    assert candidate.payload.schema_version == "movement_payload.v1"
    assert candidate.payload.unit == "index"


@pytest.mark.parametrize(
    ("result", "expected_state"),
    (
        (
            normalize_current(
                {"smbdFlag": 1},
                provider_account_id="semantic-account",
                provider_device=DEVICE,
                raw_sha256="d" * 64,
                requested_at=AT,
                received_at=RECEIVED,
            ),
            "in_bed",
        ),
        (
            normalize_realtime(
                {"probStatus": 6, "time": str(int(AT.timestamp() * 1000))},
                provider_account_id="semantic-account",
                provider_device=DEVICE,
                raw_sha256="e" * 64,
                requested_at=AT,
                received_at=RECEIVED,
                binding_timezone_name="Asia/Shanghai",
            ),
            "out_of_bed",
        ),
    ),
)
def test_explicit_v2_accepts_proved_vendor_derived_pull_bed_presence(
    result,
    expected_state: str,
) -> None:
    canonical = canonicalize_pull_result_v2(result)

    assert len(canonical) == 1
    assert canonical[0].metric_id == "bed_presence"
    assert canonical[0].source_kind is SourceKind.VENDOR_DERIVED
    assert canonical[0].payload.state.value == expected_state
    assert canonical[0].ontology_version == "sleep_observation_ontology.v2"


def test_v2_bed_presence_ontology_remains_fail_closed_for_unproved_sources() -> None:
    result = normalize_current(
        {"smbdFlag": 1},
        provider_account_id="semantic-account",
        provider_device=DEVICE,
        raw_sha256="f" * 64,
        requested_at=AT,
        received_at=RECEIVED,
    )
    unsupported = result.candidates[0].model_copy(
        update={"source_kind": SourceKind.USER_REPORTED}
    )

    with pytest.raises(CanonicalObservationRejected) as rejected:
        CanonicalObservationFactoryV2().build(
            candidate=unsupported,
            normalizer_version="test.v1",
        )
    assert (
        rejected.value.category
        is SemanticRejectionCategory.INVALID_SOURCE_PROVENANCE
    )


def test_explicit_v2_uses_one_factory_and_failure_never_falls_back() -> None:
    candidate = _push_movement()
    with patch.object(
        CanonicalObservationFactoryV2,
        "build",
        wraps=CanonicalObservationFactoryV2().build,
    ) as build:
        canonicalize_push_candidates_v2((candidate,))
    assert build.call_count == 1

    class RejectingFactory:
        def build(self, **_kwargs):
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.INVALID_UNIT,
                "forced V2 rejection",
            )

    with pytest.raises(CanonicalObservationRejected) as rejected:
        canonicalize_push_candidates_v2((candidate,), factory=RejectingFactory())
    assert rejected.value.category is SemanticRejectionCategory.INVALID_UNIT


def test_replay_feature_switch_emits_matching_versioned_ingress_contract() -> None:
    scenario = load_packaged_scenario("normal-one-night")
    generated = CanonicalReplayGenerator().generate(scenario)
    legacy = ReplayExternalFactAdapter().adapt(scenario, generated)
    canonical = ReplayExternalFactAdapter(
        observation_semantics_version="v2"
    ).adapt(scenario, generated)

    assert all(
        item.observation.schema_version == "replay_observation_input.v1"
        for item in legacy.items
    )
    assert all(
        item.observation.schema_version == "replay_observation_input.v2"
        for item in canonical.items
    )
    movement = next(
        item.observation
        for item in canonical.items
        if item.observation.observation_type is ObservationType.MOVEMENT
    )
    assert isinstance(movement, ReplayObservationInputV2)
    assert movement.movement_payload_v2 is not None
    assert (
        movement.movement_payload_v2.metric_id
        is MovementMetricId.MOVEMENT_INDEX
    )


def test_invalid_feature_version_fails_closed() -> None:
    with pytest.raises(ValueError):
        ObservationSemanticsVersion("v3")
