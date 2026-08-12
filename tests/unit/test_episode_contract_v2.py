from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from sleepagent.sleep_domain import DataMode
from sleepagent.sleep_domain.episode_v2 import (
    EpisodeAssignmentBasis,
    EpisodeDateConfidence,
    EpisodePublicationStatus,
    UUID7Generator,
    finalize_episode_date,
    open_episode_v2,
    upcast_night_episode,
    uuid7_from_parts,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
START = datetime(2026, 8, 7, 15, 30, tzinfo=UTC)
DEADLINE = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)


def _open(**overrides):
    values = {
        "namespace_id": "replay:episode-v2",
        "namespace_generation": 1,
        "data_mode": DataMode.REPLAY,
        "run_id": "run-1",
        "arm_id": "arm-control",
        "subject_id": "subject-1",
        "opening_source_idempotency_identity": "bed-event-opaque-1",
        "timezone_name": "Asia/Shanghai",
        "boundary_policy_version": "wake-date.v2",
        "collection_start_at": START,
        "deterministic_close_deadline_at": DEADLINE,
        "bed_at": START,
        "id_generator": lambda _now: uuid7_from_parts(
            1_786_119_000_000,
            12345,
        ),
    }
    values.update(overrides)
    return open_episode_v2(**values)


def test_uuid7_is_rfc_variant_and_clock_rollback_is_monotonic() -> None:
    generator = UUID7Generator()
    first = UUID(generator(datetime(2026, 8, 7, tzinfo=UTC)))
    second = UUID(generator(datetime(2026, 8, 6, tzinfo=UTC)))

    assert first.version == second.version == 7
    assert first.variant == second.variant == "specified in RFC 4122"
    assert second.int > first.int
    assert (second.int >> 80) == (first.int >> 80)


def test_uuid7_deterministic_constructor_keeps_random_space_distinct() -> None:
    first = uuid7_from_parts(1_700_000_000_000, 1)
    second = uuid7_from_parts(1_700_000_000_000, 2)

    assert first != second
    assert UUID(first).version == UUID(second).version == 7


def test_uuid7_is_unique_across_independent_processes_at_one_timestamp() -> None:
    script = """
import json
from datetime import datetime, timezone
from sleepagent.sleep_domain.episode_v2 import UUID7Generator

generator = UUID7Generator()
instant = datetime(2026, 8, 7, tzinfo=timezone.utc)
print(json.dumps([generator(instant) for _ in range(128)]))
"""
    batches = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        for _ in range(4)
    ]
    generated = [value for batch in batches for value in batch]

    assert len(generated) == 512
    assert len(set(generated)) == len(generated)
    assert all(UUID(value).version == 7 for value in generated)
    assert all(
        UUID(value).variant == "specified in RFC 4122" for value in generated
    )


def test_open_identity_does_not_depend_on_bed_or_wake_date() -> None:
    first = _open()
    second = _open(
        collection_start_at=START + timedelta(hours=2),
        bed_at=START + timedelta(hours=2),
    )

    assert first.episode_anchor_key == second.episode_anchor_key
    assert first.episode_local_date is None
    assert first.assignment_basis == EpisodeAssignmentBasis.PROVISIONAL
    assert first.bed_local_date == date(2026, 8, 7)


def test_observed_wake_date_is_canonical_even_across_midnight() -> None:
    episode = _open()
    wake = datetime(2026, 8, 8, 0, 30, tzinfo=UTC)

    finalized = finalize_episode_date(
        episode,
        wake_at=wake,
        committed_at=wake + timedelta(minutes=1),
    )

    assert finalized.bed_local_date == date(2026, 8, 7)
    assert finalized.wake_local_date == date(2026, 8, 8)
    assert finalized.episode_local_date == date(2026, 8, 8)
    assert finalized.assignment_basis == EpisodeAssignmentBasis.OBSERVED_WAKE
    assert finalized.date_confidence == EpisodeDateConfidence.OBSERVED


def test_missing_wake_uses_explicit_estimated_deadline_fallback() -> None:
    finalized = finalize_episode_date(
        _open(),
        committed_at=DEADLINE,
    )

    assert finalized.episode_local_date == date(2026, 8, 8)
    assert finalized.assignment_basis == EpisodeAssignmentBasis.DEADLINE_FALLBACK
    assert finalized.date_confidence == EpisodeDateConfidence.ESTIMATED


def test_date_conflict_creates_non_publishable_candidate_revision() -> None:
    finalized = finalize_episode_date(
        _open(),
        wake_at=datetime(2026, 8, 8, 0, 30, tzinfo=UTC),
        committed_at=datetime(2026, 8, 8, 0, 31, tzinfo=UTC),
        canonical_date_available=lambda _value: False,
    )

    assert finalized.date_conflict is True
    assert (
        finalized.publication_status
        == EpisodePublicationStatus.RECONCILIATION_REQUIRED
    )
    assert finalized.current_revision == 2


def test_timezone_is_frozen_for_episode_date_assignment() -> None:
    episode = _open(timezone_name="America/New_York")
    wake = datetime(2026, 8, 8, 3, 30, tzinfo=UTC)

    finalized = finalize_episode_date(
        episode,
        wake_at=wake,
        committed_at=wake,
    )

    assert finalized.wake_local_date == date(2026, 8, 7)
    assert finalized.episode_local_date == date(2026, 8, 7)


def test_legacy_upcaster_never_mislabels_bed_date_as_observed_wake() -> None:
    upcast = upcast_night_episode(
        {
            "schema_version": "night_episode.v1",
            "night_episode_id": "legacy-night-id",
            "namespace_id": "replay:episode-v2",
            "namespace_generation": 1,
            "run_id": "run-1",
            "arm_id": "arm-control",
            "data_mode": "replay",
            "subject_id": "subject-1",
            "timezone_name": "Asia/Shanghai",
            "night_key": "legacy-night-key",
            "local_sleep_date": "2026-08-07",
            "collection_start_at": START.isoformat(),
            "report_deadline_at": DEADLINE.isoformat(),
            "created_at": START.isoformat(),
            "updated_at": START.isoformat(),
        }
    )

    assert upcast.legacy_local_sleep_date == date(2026, 8, 7)
    assert upcast.episode_local_date is None
    assert upcast.assignment_basis == EpisodeAssignmentBasis.LEGACY_UNKNOWN


def test_naive_episode_instants_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone offset"):
        _open(bed_at=datetime(2026, 8, 7, 23, 0))
