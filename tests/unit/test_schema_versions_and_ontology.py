from __future__ import annotations

import pytest

from sleepagent.sleep_domain.contracts import (
    AvailabilityState,
    HeartRatePayload,
    ObservationType,
    SourceKind,
    VendorSleepProfileMetricPayload,
)
from sleepagent.sleep_domain.ontology import (
    ONTOLOGY_VERSION,
    canonical_source_kind,
    validate_observation_ontology,
)
from sleepagent.sleep_domain.schema_versions import (
    VersionDispatchError,
    dispatch_versioned,
    dispatch_versioned_json,
)


pytestmark = pytest.mark.unit


def test_version_dispatch_reads_only_the_declared_schema() -> None:
    readers = {"thing.v1": lambda payload: str(payload["value"])}

    assert dispatch_versioned(
        {"schema_version": "thing.v1", "value": "ok"},
        family="thing",
        readers=readers,
    ) == "ok"
    assert dispatch_versioned_json(
        b'{"schema_version":"thing.v1","value":"json"}',
        family="thing",
        readers=readers,
    ) == "json"
    with pytest.raises(VersionDispatchError, match="unsupported"):
        dispatch_versioned(
            {"schema_version": "thing.v2", "value": "no"},
            family="thing",
            readers=readers,
        )
    with pytest.raises(VersionDispatchError, match="schema_version is required"):
        dispatch_versioned({}, family="thing", readers=readers)


def test_metric_unit_and_source_ontology_is_fail_closed() -> None:
    assert ONTOLOGY_VERSION == "sleep_observation_ontology.v1"
    validate_observation_ontology(
        observation_type=ObservationType.HEART_RATE,
        payload=HeartRatePayload(value=62.0),
        source_kind=SourceKind.DEVICE_MEASURED,
    )
    validate_observation_ontology(
        observation_type=ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
        payload=VendorSleepProfileMetricPayload(
            metric_name="vendor_time_in_bed_minutes",
            value_state=AvailabilityState.KNOWN,
            value=480,
            unit="minutes",
        ),
        source_kind=SourceKind.VENDOR_DERIVED,
    )
    with pytest.raises(ValueError, match="requires device_measured"):
        validate_observation_ontology(
            observation_type=ObservationType.HEART_RATE,
            payload=HeartRatePayload(value=62.0),
            source_kind=SourceKind.USER_REPORTED,
        )


def test_family_and_elder_vocabulary_never_collapses_source_authority() -> None:
    assert canonical_source_kind("elder_self_report") == SourceKind.USER_REPORTED
    assert (
        canonical_source_kind("family_observation")
        == SourceKind.EXTERNALLY_REPORTED
    )
    assert canonical_source_kind("objective_sensor") == SourceKind.DEVICE_MEASURED
    assert len(
        {
            canonical_source_kind("elder_self_report"),
            canonical_source_kind("family_observation"),
            canonical_source_kind("objective_sensor"),
        }
    ) == 3
    with pytest.raises(ValueError, match="unsupported source vocabulary"):
        canonical_source_kind("family_confirmed_as_objective")
