from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.application.acquisition import PostgresAcquisitionScheduler
from sleepagent.application.acquisition import AcquisitionSchedule
from sleepagent.application.night_finalization import (
    NightFinalizationPolicy,
    NightFinalizationService,
    _decide,
)
from sleepagent.integrations.perceptor.client import PlatformApiError
from sleepagent.integrations.perceptor.pull_ingestion import (
    PerceptorHistoryBackfillChunkResult,
    PerceptorPullIngressResult,
)
from sleepagent.integrations.perceptor.scheduled import (
    _run_scheduled_recent_history,
    _scheduled_instant,
)
from sleepagent.workers.kernel import RetryableWorkError
from sleepagent.workers.acquisition import _scheduled_acquisition_error_code


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


def test_scheduled_history_boundaries_drop_microseconds_deterministically() -> None:
    instant = _scheduled_instant(
        {"scheduled_for": "2026-09-05T01:02:03.987654+08:00"}
    )

    assert instant.isoformat() == "2026-09-05T01:02:03+08:00"
    assert (instant - timedelta(minutes=15)).microsecond == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        _scheduled_instant({"scheduled_for": "2026-09-05T01:02:03"})


def test_scheduled_recent_history_uses_isolated_window_not_rolling_checkpoint() -> None:
    instant = datetime(
        2026, 9, 6, 14, 15, 19, tzinfo=timezone(timedelta(hours=8))
    )
    ingress = PerceptorPullIngressResult(
        disposition="accepted",
        raw_ingress_record_id="raw-recent-1",
        normalization_work_id="work-recent-1",
        subject_id="subject-1",
        device_binding_id="binding-1",
        duplicate=False,
        batch_identity="batch-recent-1",
        response_semantic_sha256="a" * 64,
    )

    class Runner:
        calls: list[tuple[datetime, datetime]] = []

        def pull_history(self, **kwargs: object) -> object:
            del kwargs
            raise AssertionError("scheduled recent path must not consume rolling state")

        def backfill_history(
            self, *, start_at: datetime, end_at: datetime
        ) -> tuple[PerceptorHistoryBackfillChunkResult, ...]:
            self.calls.append((start_at, end_at))
            return (
                PerceptorHistoryBackfillChunkResult(
                    window_start_at=start_at,
                    window_end_at=end_at,
                    status="succeeded",
                    vendor_record_count=2,
                    raw_series_sample_count=17,
                    ingress=ingress,
                ),
            )

    runner = Runner()
    result = _run_scheduled_recent_history(runner, instant=instant)  # type: ignore[arg-type]

    assert runner.calls == [(instant - timedelta(minutes=15), instant)]
    assert (runner.calls[0][1] - runner.calls[0][0]).total_seconds() == 900
    assert all(value.microsecond == 0 for value in runner.calls[0])
    assert result == {
        "disposition": "accepted",
        "raw_ingress_record_id": "raw-recent-1",
        "normalization_work_id": "work-recent-1",
        "duplicate": False,
        "history_window_start": (instant - timedelta(minutes=15)).isoformat(),
        "history_window_end": instant.isoformat(),
        "checkpoint_source": "isolated_scheduled_recent_window",
        "vendor_record_count": 2,
        "raw_series_sample_count": 17,
    }


def test_scheduled_recent_history_retries_failed_isolated_request() -> None:
    instant = datetime(2026, 9, 6, 14, 15, 19, tzinfo=UTC)

    class Runner:
        def backfill_history(
            self, *, start_at: datetime, end_at: datetime
        ) -> tuple[PerceptorHistoryBackfillChunkResult, ...]:
            return (
                PerceptorHistoryBackfillChunkResult(
                    window_start_at=start_at,
                    window_end_at=end_at,
                    status="failed",
                    vendor_record_count=0,
                    raw_series_sample_count=0,
                    ingress=None,
                    error_code="platform_api_timeout",
                ),
            )

    with pytest.raises(RetryableWorkError, match="platform_api_timeout") as failure:
        _run_scheduled_recent_history(Runner(), instant=instant)  # type: ignore[arg-type]
    assert failure.value.retry_after_seconds == 60


def test_finalization_scan_closes_deadline_episode_before_discovery() -> None:
    events: list[str] = []

    class RecordingService(NightFinalizationService):
        def __init__(self) -> None:
            pass

        def close_overdue_for_binding(self, *args: object, **kwargs: object) -> None:
            events.append("close")

        def _discover_due_episode_ids(
            self, *args: object, **kwargs: object
        ) -> tuple[str, ...]:
            events.append("discover")
            return ()

    service = RecordingService()

    assert service.finalize_due_for_binding(
        object(),  # type: ignore[arg-type]
        device_binding_id="binding-1",
        evaluated_at=DEADLINE,
    ) == ()
    assert events == ["close", "discover"]


def test_scheduled_provider_failure_code_retains_safe_structure() -> None:
    error = PlatformApiError(
        "DEVICE_OFFLINE",
        endpoint="/vitalSigns/getHistoryData",
        http_status=429,
        vendor_code="6001",
        vendor_message="device offline",
    )

    assert _scheduled_acquisition_error_code(error) == (
        "platform_api_device_offline_http_429_vendor_6001"
    )
    assert "device offline" not in _scheduled_acquisition_error_code(error)
