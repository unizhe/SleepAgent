from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

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
    SleepObservation,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.domain.observation_semantics import (
    MovementMetricId,
    MovementPayloadV2,
    aggregate_movement_semantics_v2,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    LifecycleSnapshotRecord,
    LoadedNormalizationWork,
    NormalizationHandler,
    NormalizationLease,
    RawPayloadCipher,
    ReplayObservationInputV2,
    _raw_aad,
)
from sleepagent.application.product_data import ProductRevisionFacts
from sleepagent.integrations.perceptor.pull import (
    normalize_history,
    normalize_sleep_report,
)
from sleepagent.persistence.observation_semantics_upcast import (
    classify_legacy_movement,
)
from sleepagent.persistence.uow import UowScope


pytestmark = pytest.mark.unit
UTC = timezone.utc
AT = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
DEVICE = ProviderDeviceIdentity(provider_device_id="semantic-device")


def _observation(candidate: object, suffix: str) -> SleepObservation:
    return SleepObservation(
        observation_id=f"observation:{suffix}",
        data_mode=candidate.data_mode,
        observation_type=candidate.observation_type,
        payload=candidate.payload,
        subject_id="subject:semantic",
        device_id="device:semantic",
        device_binding_id="binding:semantic",
        binding_version=1,
        request_signed_at=candidate.request_signed_at,
        measurement_at=candidate.measurement_at,
        event_occurred_at=candidate.event_occurred_at,
        received_at=candidate.received_at,
        source_timestamp_text=candidate.source_timestamp_text,
        timezone_status=candidate.timezone_status,
        source_kind=candidate.source_kind,
        quality=candidate.quality,
        provenance=candidate.provenance,
        source_key=candidate.source_key,
        idempotency_key=candidate.idempotency_key,
    )


def _context() -> dict[str, object]:
    return {
        "provider_account_id": "semantic-account",
        "provider_device": DEVICE,
        "raw_sha256": "c" * 64,
        "requested_at": AT,
        "received_at": AT + timedelta(seconds=1),
    }


def test_legacy_upcast_uses_vendor_evidence_and_never_numeric_heuristics() -> None:
    history = normalize_history(
        [
            {
                "device_id": "semantic-device",
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": "12.5",
                "send_time": "2026-08-23T08:00:00",
            }
        ],
        binding_timezone_name="UTC",
        **_context(),
    )
    index_candidate = next(
        item
        for item in history.candidates
        if item.observation_type is ObservationType.MOVEMENT
    )
    hourly_candidate = normalize_sleep_report(
        {"body_shake_data": [{"hour": 8, "count": 21}]},
        report_date=date(2026, 8, 23),
        binding_timezone_name="UTC",
        **_context(),
    ).candidates[0]
    ambiguous_candidate = hourly_candidate.model_copy(
        update={
            "payload": MovementPayload(value=21),
            "measurement_at": AT,
            "source_timestamp_text": str(int(AT.timestamp())),
            "quality": hourly_candidate.quality.model_copy(
                update={
                    "quality_flags": ("vendor_report_series",),
                    "limitations": (),
                }
            ),
        }
    )

    index = classify_legacy_movement(
        _observation(index_candidate, "index"), index_candidate
    )
    count = classify_legacy_movement(
        _observation(hourly_candidate, "count"), hourly_candidate
    )
    ambiguous = classify_legacy_movement(
        _observation(ambiguous_candidate, "ambiguous"), ambiguous_candidate
    )

    assert index.metric_id == "movement_index"
    assert index.upcast_status == "legacy_classified"
    assert count.metric_id == "movement_event_count"
    assert count.aggregation_end_at - count.aggregation_start_at == timedelta(
        hours=1
    )
    assert ambiguous.metric_id == "legacy_ambiguous_movement"
    assert ambiguous.trusted_for_analytics is False
    assert ambiguous.classification_evidence["numeric_heuristic_used"] is False


def test_metric_safe_aggregation_separates_index_count_and_ambiguity() -> None:
    result = aggregate_movement_semantics_v2(
        (
            {
                "metric_id": "movement_index",
                "unit": "vendor_index",
                "value": 0.8,
                "source_kind": "device_measured",
                "trusted_for_analytics": True,
            },
            {
                "metric_id": "movement_index",
                "unit": "vendor_index",
                "value": 1.2,
                "source_kind": "device_measured",
                "trusted_for_analytics": True,
            },
            {
                "metric_id": "movement_event_count",
                "unit": "count",
                "value": 21,
                "aggregation_start_at": AT,
                "aggregation_end_at": AT + timedelta(hours=1),
                "source_kind": "vendor_derived",
                "trusted_for_analytics": True,
            },
            {
                "metric_id": "legacy_ambiguous_movement",
                "unit": "legacy_unknown",
                "value": 20,
                "trusted_for_analytics": False,
            },
        )
    )

    assert result["movement_index"] == {
        "unit": "vendor_index",
        "sample_count": 2,
        "mean": 1.0,
        "median": 1.0,
    }
    assert result["movement_event_count"]["total"] == 21
    assert result["movement_event_count"]["hourly_max"] == 21
    assert result["excluded"]["legacy_ambiguous_count"] == 1
    assert result["threshold_status"] == "SEMANTIC_THRESHOLD_UNRESOLVED"


def test_index_only_aggregation_keeps_count_result_empty() -> None:
    result = aggregate_movement_semantics_v2(
        (
            {
                "metric_id": "movement_index",
                "unit": "vendor_index",
                "value": 2.5,
                "source_kind": "device_measured",
                "trusted_for_analytics": True,
            },
        )
    )

    assert result["movement_index"]["mean"] == 2.5
    assert result["movement_event_count"]["total"] is None


def test_count_only_aggregation_is_interval_aware() -> None:
    result = aggregate_movement_semantics_v2(
        (
            {
                "metric_id": "movement_event_count",
                "unit": "count",
                "value": 4,
                "aggregation_start_at": AT,
                "aggregation_end_at": AT + timedelta(minutes=30),
                "source_kind": "vendor_derived",
                "trusted_for_analytics": True,
            },
            {
                "metric_id": "movement_event_count",
                "unit": "count",
                "value": 7,
                "aggregation_start_at": AT + timedelta(minutes=30),
                "aggregation_end_at": AT + timedelta(hours=1, minutes=30),
                "source_kind": "vendor_derived",
                "trusted_for_analytics": True,
            },
        )
    )

    count = result["movement_event_count"]
    assert count["total"] == 11
    assert count["hourly_max"] == 7
    assert count["hourly_window_count"] == 1
    assert count["non_hourly_window_count"] == 1
    assert "NON_HOURLY_COUNT_EXCLUDED_FROM_HOURLY_MAX" in result["reason_codes"]


def test_ambiguous_only_aggregation_is_not_coerced_to_zero() -> None:
    result = aggregate_movement_semantics_v2(
        (
            {
                "metric_id": "legacy_ambiguous_movement",
                "unit": "legacy_unknown",
                "value": 41,
                "source_kind": "vendor_derived",
                "trusted_for_analytics": False,
            },
        )
    )

    assert result["movement_index"]["mean"] is None
    assert result["movement_event_count"]["total"] is None
    assert result["excluded"]["legacy_ambiguous_count"] == 1


def test_event_count_partial_coverage_remains_visible() -> None:
    result = aggregate_movement_semantics_v2(
        (
            {
                "metric_id": "movement_event_count",
                "unit": "count",
                "value": 4,
                "aggregation_start_at": AT,
                "aggregation_end_at": AT + timedelta(hours=1),
                "source_kind": "vendor_derived",
                "trusted_for_analytics": True,
            },
        ),
        expected_start_at=AT,
        expected_end_at=AT + timedelta(hours=2),
    )

    assert result["movement_event_count"]["total"] == 4
    assert result["movement_event_count"]["partial_coverage"] is True
    assert "PARTIAL_COUNT_COVERAGE" in result["reason_codes"]


def test_count_aggregation_excludes_duplicate_and_overlapping_windows() -> None:
    def fact(start: datetime, end: datetime, value: int) -> dict[str, object]:
        return {
            "metric_id": "movement_event_count",
            "unit": "count",
            "value": value,
            "aggregation_start_at": start,
            "aggregation_end_at": end,
            "source_kind": "vendor_derived",
            "trusted_for_analytics": True,
        }

    result = aggregate_movement_semantics_v2(
        (
            fact(AT, AT + timedelta(hours=1), 4),
            fact(AT, AT + timedelta(hours=1), 4),
            fact(AT + timedelta(hours=2), AT + timedelta(hours=3), 8),
            fact(
                AT + timedelta(hours=2, minutes=30),
                AT + timedelta(hours=3, minutes=30),
                9,
            ),
            fact(AT + timedelta(hours=4), AT + timedelta(hours=5), 10),
        )
    )

    count = result["movement_event_count"]
    assert count["total"] == 14
    assert count["duplicate_observation_count"] == 1
    assert count["overlapping_window_count"] == 2
    assert count["partial_coverage"] is True


def test_product_v2_facts_do_not_recreate_legacy_mixed_average() -> None:
    observations = (
        {
            "payload": {"observation_type": "movement", "value": 0.8},
            "observation_semantics_v2": {
                "metric_id": "movement_index",
                "unit": "vendor_index",
                "value": 0.8,
                "source_kind": "device_measured",
                "trusted_for_analytics": True,
            },
        },
        {
            "payload": {"observation_type": "movement", "value": 41},
            "observation_semantics_v2": {
                "metric_id": "movement_event_count",
                "unit": "count",
                "value": 41,
                "aggregation_start_at": AT.isoformat(),
                "aggregation_end_at": (AT + timedelta(hours=1)).isoformat(),
                "source_kind": "vendor_derived",
                "trusted_for_analytics": True,
            },
        },
    )
    facts = ProductRevisionFacts(
        night_episode_id="night:v2",
        night_episode_revision_id="revision:v2",
        night_episode_revision_number=1,
        subject_id="subject:v2",
        data_mode=DataMode.LIVE,
        timezone_name="UTC",
        local_sleep_date="2026-08-23",
        data_sufficiency="sufficient",
        canonical_observations=observations,
        deterministic_quality={"coverage_ratio": 1.0},
        deterministic_risk={"risk_state": "no_reviewed_signal"},
        conflict_summaries=(),
        provenance_references=("revision:v2",),
        canonical_data_version="a" * 64,
    )

    summary = facts.deterministic_night_summary()
    assert "movement" not in summary["vital_centers"]
    assert summary["movement_semantics_v2"]["movement_index"]["mean"] == 0.8
    assert summary["movement_semantics_v2"]["movement_event_count"]["total"] == 41
    assert "20.9" not in str(summary)
    evidence = facts.tool_inputs()["radar.get_night_evidence"]["data"]
    assert evidence["deterministic_night_summary"] == summary
    assert "movement" not in evidence["deterministic_night_summary"]["vital_centers"]


def test_product_partial_v2_revision_fails_closed_for_unclassified_movement() -> None:
    facts = ProductRevisionFacts(
        night_episode_id="night:partial-v2",
        night_episode_revision_id="revision:partial-v2",
        night_episode_revision_number=1,
        subject_id="subject:partial-v2",
        data_mode=DataMode.LIVE,
        timezone_name="UTC",
        local_sleep_date="2026-08-23",
        data_sufficiency="sufficient",
        canonical_observations=(
            {
                "payload": {"observation_type": "heart_rate", "value": 60},
                "observation_semantics_v2": {
                    "metric_id": "heart_rate",
                    "value": 60,
                    "unit": "beats_per_minute",
                    "source_kind": "device_measured",
                    "trusted_for_analytics": True,
                },
            },
            {
                "payload": {"observation_type": "movement", "value": 41},
            },
        ),
        deterministic_quality={"coverage_ratio": 1.0},
        deterministic_risk={"risk_state": "no_reviewed_signal"},
        conflict_summaries=(),
        provenance_references=("revision:partial-v2",),
        canonical_data_version="b" * 64,
    )

    summary = facts.deterministic_night_summary()
    assert "movement" not in summary["vital_centers"]
    assert summary["movement_semantics_v2"]["movement_index"]["mean"] is None
    assert summary["movement_semantics_v2"]["movement_event_count"]["total"] is None
    assert summary["movement_semantics_v2"]["excluded"]["unclassified_v1_count"] == 1


def test_replay_v2_handler_retains_semantics_through_repository_handoff() -> None:
    quality = ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
    )
    source = ReplayObservationInputV2(
        provider_id="perceptor",
        provider_account_id="semantic-account",
        provider_device_id="semantic-device",
        subject_id="subject:semantic",
        device_id="device:semantic",
        device_binding_id="binding:semantic",
        binding_version=1,
        timezone_name="UTC",
        observation_type=ObservationType.MOVEMENT,
        payload=MovementPayload(value=1.25),
        movement_payload_v2=MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_INDEX,
            value=1.25,
            unit="vendor_index",
            vendor_semantic_code="perceptor.body_shake.index",
        ),
        source_kind=SourceKind.DEVICE_MEASURED,
        quality=quality,
        measurement_at=AT,
        received_at=AT + timedelta(seconds=1),
        timezone_status=TimezoneStatus.KNOWN,
        source_key="replay:v2:movement",
        idempotency_identity="replay:v2:movement",
    )
    scope = UowScope(
        namespace_id="replay:semantic",
        namespace_generation=1,
        data_mode="replay",
        run_id="run:semantic",
        arm_id="arm:semantic",
        process_role="worker",
        purpose="durable_work",
        service_principal_id="worker:semantic",
        subject_id="subject:semantic",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker:semantic",
    )
    raw_id = "raw:semantic"
    raw = source.canonical_bytes()
    cipher = RawPayloadCipher(b"k" * 32, key_id="test-key")
    work = LoadedNormalizationWork(
        work_id="work:semantic",
        raw_ingress_record_id=raw_id,
        encrypted_payload=cipher.encrypt(raw, aad=_raw_aad(scope, raw_id)),
        encryption_key_id="test-key",
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        subject_id="subject:semantic",
    )

    class Repository:
        persisted: dict[str, object] | None = None

        def load_normalization_work(self, _lease: object) -> LoadedNormalizationWork:
            return work

        def lock_subject_lifecycle(self) -> None:
            return None

        def load_lifecycle(self) -> LifecycleSnapshotRecord:
            return LifecycleSnapshotRecord(None, "dormant", 0, None, None)

        def load_closed_episode_candidates(
            self, **_kwargs: object
        ) -> tuple[object, ...]:
            return ()

        def find_conflicting_episode(self, **_kwargs: object) -> None:
            return None

        def persist_normalization_handoff(self, **kwargs: object) -> None:
            self.persisted = kwargs

    class Uow:
        connection = object()
        committed = False

        def __enter__(self) -> "Uow":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def commit(self) -> None:
            self.committed = True

    class Factory:
        def __init__(self, uow: Uow) -> None:
            self.uow = uow

        def begin(self, _scope: object) -> Uow:
            return self.uow

    repository = Repository()
    uow = Uow()
    ids = iter(("observation:semantic", "candidate:semantic"))
    handler = NormalizationHandler(
        Factory(uow),  # type: ignore[arg-type]
        cipher=cipher,
        id_generator=lambda _at=None: next(ids),
        now_factory=lambda: AT + timedelta(seconds=2),
        repository_factory=(
            lambda _connection, _scope: repository
        ),  # type: ignore[arg-type]
        observation_semantics_version="v2",
    )

    handler.process(
        scope,
        NormalizationLease(
            work_id=work.work_id,
            lease_generation=1,
            fencing_token="f" * 32,
            worker_instance="worker:semantic",
        ),
    )

    assert uow.committed is True
    assert repository.persisted is not None
    semantic = repository.persisted["canonical_semantics"]
    assert semantic.metric_id == "movement_index"
    assert semantic.compatibility_candidate.payload.schema_version == (
        "movement_payload.v1"
    )
