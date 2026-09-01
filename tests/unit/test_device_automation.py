from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.application.acquisition import PostgresAcquisitionScheduler
from sleepagent.application.acquisition import AcquisitionSchedule
from sleepagent.application.night_finalization import (
    NightFinalizationPolicy,
    _decide,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
DEADLINE = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)


def _evidence(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "date_conflict": False,
        "deadline_at": DEADLINE,
        "observation_count": 4,
        "source_report_version_id": None,
        "source_report_is_empty": None,
    }
    value.update(changes)
    return value


def test_soft_finalization_is_explicitly_provisional_after_grace() -> None:
    decision = _decide(
        _evidence(),
        now=DEADLINE + timedelta(hours=3),
        policy=NightFinalizationPolicy(),
    )

    assert decision == {
        "state": "soft_finalized",
        "provisional": True,
        "coverage_status": "partial",
        "coverage_caveat": "Vendor SleepReport is pending; coverage is provisional.",
        "revision_cause": "wake_grace_elapsed",
    }


def test_vendor_report_supports_direct_open_to_hard() -> None:
    decision = _decide(
        _evidence(
            source_report_version_id="report-v1",
            source_report_is_empty=False,
        ),
        now=DEADLINE,
        policy=NightFinalizationPolicy(),
    )

    assert decision is not None
    assert decision["state"] == "hard_finalized"
    assert decision["revision_cause"] == "vendor_report_reconciled"
    assert decision["provisional"] is False


def test_maximum_wait_hard_finalizes_with_bounded_caveat() -> None:
    decision = _decide(
        _evidence(observation_count=0),
        now=DEADLINE + timedelta(hours=25),
        policy=NightFinalizationPolicy(),
    )

    assert decision is not None
    assert decision["state"] == "hard_finalized"
    assert decision["coverage_status"] == "data_insufficient"
    assert "Maximum" in str(decision["coverage_caveat"])


def test_date_conflict_requires_reconciliation() -> None:
    decision = _decide(
        _evidence(date_conflict=True),
        now=DEADLINE + timedelta(days=2),
        policy=NightFinalizationPolicy(),
    )

    assert decision is not None
    assert decision["state"] == "reconciliation_required"


def test_before_grace_remains_open() -> None:
    assert _decide(
        _evidence(),
        now=DEADLINE + timedelta(minutes=30),
        policy=NightFinalizationPolicy(),
    ) is None


class _NeverCalledFactory:
    def begin(self, scope: object) -> object:
        del scope
        raise AssertionError("disabled scheduler must not access PostgreSQL")


def test_scheduler_disabled_mode_creates_no_database_work() -> None:
    scheduler = PostgresAcquisitionScheduler(
        _NeverCalledFactory(),  # type: ignore[arg-type]
        data_mode="live",
        service_principal_id="worker-1",
        worker_instance="scheduler-1",
        enabled=False,
    )

    assert scheduler.fire_due() == ()


@pytest.mark.parametrize(("cadence", "jitter"), ((60, 60), (60, 61)))
def test_schedule_contract_rejects_jitter_at_or_above_cadence(
    cadence: int, jitter: int
) -> None:
    with pytest.raises(ValueError, match="less than cadence"):
        AcquisitionSchedule.model_validate(
            {
                "schedule_id": "schedule-1",
                "namespace_id": "live:test",
                "data_mode": "live",
                "namespace_generation": 1,
                "subject_id": "subject-1",
                "device_binding_id": "binding-1",
                "binding_version": 1,
                "job_type": "night.finalization_scan",
                "enabled": True,
                "next_run_at": DEADLINE,
                "consecutive_failures": 0,
                "cadence_seconds": cadence,
                "jitter_seconds": jitter,
                "max_attempts": 5,
                "schedule_policy_version": "test.v1",
                "schedule_policy_sha256": "a" * 64,
                "cas_version": 0,
                "created_at": DEADLINE,
                "updated_at": DEADLINE,
            }
        )


@pytest.mark.parametrize("jitter", (0, 59))
def test_schedule_contract_accepts_zero_and_near_cadence_jitter(
    jitter: int,
) -> None:
    schedule = AcquisitionSchedule.model_validate(
        {
            "schedule_id": "schedule-1",
            "namespace_id": "live:test",
            "data_mode": "live",
            "namespace_generation": 1,
            "subject_id": "subject-1",
            "device_binding_id": "binding-1",
            "binding_version": 1,
            "job_type": "night.finalization_scan",
            "enabled": True,
            "next_run_at": DEADLINE,
            "consecutive_failures": 0,
            "cadence_seconds": 60,
            "jitter_seconds": jitter,
            "max_attempts": 5,
            "schedule_policy_version": "test.v1",
            "schedule_policy_sha256": "a" * 64,
            "cas_version": 0,
            "created_at": DEADLINE,
            "updated_at": DEADLINE,
        }
    )
    assert schedule.jitter_seconds == jitter
