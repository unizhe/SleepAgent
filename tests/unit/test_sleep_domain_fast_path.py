from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sleepagent.persistence import (
    SCHEMA_VERSION,
    POSTGRES_BASELINE_SQL,
)
from sleepagent.sleep_domain import (
    AdapterObservationCandidate,
    AlertCorrelationOutcome,
    AlertLifecycleState,
    AlgorithmVersionValue,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    CandidatePromotionService,
    CareFollowupAccessPolicy,
    CareFollowupCommand,
    CareFollowupService,
    CareFollowupState,
    ConfidenceValue,
    DataMode,
    DataSufficiency,
    DeterministicFastPathService,
    DeterministicQualityPolicy,
    DeterministicRiskPolicy,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    DomainRuleReviewStatus,
    FastPathEventPolicy,
    FastPathReceiptOutcome,
    FastPathSignalType,
    HeartRatePayload,
    LifecycleTriggerKind,
    MissingIntervalPayload,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    RiskState,
    ReviewedVendorAlertRule,
    ServiceMode,
    ServiceModeProjector,
    SourceKind,
    TimezoneStatus,
    VendorAlertPayload,
)

from tests.unit.test_night_episode_lifecycle import (
    DEVICE,
    NAMESPACE,
    _admin_policy,
    _bind,
    _command,
    _observation,
    _raw,
    _raw_policy,
    _repository,
    _service,
)


ZONE = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 7, 30, 22, 0, tzinfo=ZONE)


def _canonical(
    repository,
    *,
    suffix: str,
    event_at: datetime,
    payload,
    missing_state: MissingState = MissingState.PRESENT,
    quality_flags: tuple[str, ...] = (),
    timezone_status: TimezoneStatus = TimezoneStatus.KNOWN,
) -> str:
    received_at = event_at + timedelta(seconds=1)
    raw = _raw(repository, suffix=suffix, received_at=received_at)
    source_kind = (
        SourceKind.VENDOR_DERIVED
        if isinstance(payload, VendorAlertPayload)
        else SourceKind.DEVICE_MEASURED
    )
    candidate = AdapterObservationCandidate(
        candidate_id=f"candidate:{suffix}",
        data_mode=DataMode.LIVE,
        observation_type=payload.observation_type,
        payload=payload,
        source_kind=source_kind,
        provider_id="perceptor",
        provider_account_id="perceptor-account",
        provider_device=DEVICE,
        measurement_at=event_at,
        event_occurred_at=event_at,
        received_at=received_at,
        source_timestamp_text=event_at.isoformat(),
        timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=missing_state,
            confidence=ConfidenceValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            calibration=CalibrationValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            quality_flags=quality_flags,
        ),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="perceptor-account",
            adapter_id="perceptor-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
        ),
        source_key=f"source:{suffix}",
        idempotency_key=f"observation:{suffix}",
    )
    promoted = CandidatePromotionService(
        repository,
        access_policy=_admin_policy(),
    ).promote(
        NAMESPACE,
        candidate,
        attempt_id=suffix,
        actor_id="normalizer",
        occurred_at=received_at,
    )
    assert promoted.observation is not None
    return promoted.observation.observation_id


def _open_episode(repository, *, suffix: str = "fast-path"):
    _bind(
        repository,
        timezone_name="Asia/Shanghai",
        effective_from=START - timedelta(days=1),
    )
    episode_service = _service(repository, minimum_dwell=0)
    start_id = _observation(
        repository,
        suffix=f"{suffix}:start",
        event_at=START,
        bed_state=BedPresenceState.IN_BED,
    )
    opened = episode_service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=START + timedelta(seconds=2),
    )
    assert opened.episode is not None
    return episode_service, opened.episode


def _associate(episode_service, observation_id: str, at: datetime) -> None:
    result = episode_service.associate_observation(
        NAMESPACE,
        observation_id=observation_id,
        associated_at=at + timedelta(seconds=2),
    )
    assert result.episode is not None


def _fast_path(
    repository,
    *,
    minimum_coverage_ratio: float = 0.75,
    stale_after_seconds: int = 180,
    offline_after_seconds: int = 300,
    cooldown_seconds: int = 900,
    clock_invalid_flags: tuple[str, ...] | None = None,
) -> DeterministicFastPathService:
    return DeterministicFastPathService(
        repository,
        quality_policies={
            "quality-v1": DeterministicQualityPolicy(
                policy_version="quality-v1",
                coverage_cadence_seconds=60,
                minimum_coverage_ratio=minimum_coverage_ratio,
                partial_coverage_ratio=min(0.5, minimum_coverage_ratio),
                stale_after_seconds=stale_after_seconds,
                offline_after_seconds=offline_after_seconds,
                **(
                    {}
                    if clock_invalid_flags is None
                    else {"clock_invalid_flags": clock_invalid_flags}
                ),
            )
        },
        risk_policies={
            "risk-v1": DeterministicRiskPolicy(policy_version="risk-v1")
        },
        event_policies={
            "fast-path-event-v1": FastPathEventPolicy(
                policy_version="fast-path-event-v1",
                recovery_hysteresis_observations=2,
                bed_hysteresis_observations=2,
                cooldown_seconds=cooldown_seconds,
            )
        },
    )


def _fill_coverage(repository, episode_service, *, through_minute: int = 10):
    for minute in range(1, through_minute + 1):
        event_at = START + timedelta(minutes=minute)
        observation_id = _observation(
            repository,
            suffix=f"coverage:{minute}",
            event_at=event_at,
        )
        _associate(episode_service, observation_id, event_at)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("coverage", "coverage_below_minimum"),
        ("stale", "stale_observation_stream"),
        ("offline", "device_offline_or_stream_gap"),
        ("clock", "clock_or_timezone_invalid"),
        ("missing", "explicit_missing_interval"),
    ],
)
def test_quality_failures_never_become_normal_or_all_clear(
    case: str,
    expected_reason: str,
) -> None:
    repository, connection, _ = _repository()
    episode_service, episode = _open_episode(
        repository,
        suffix=f"quality:{case}",
    )
    minimum = 0.75
    stale_after = 180
    offline_after = 300
    assessed_at = START + timedelta(minutes=10)
    if case in {"clock", "missing", "offline"}:
        _fill_coverage(repository, episode_service)
    if case == "stale":
        minimum = 0
        stale_after = 60
        offline_after = 600
        assessed_at = START + timedelta(minutes=2)
    elif case == "coverage":
        stale_after = 3600
        offline_after = 7200
    elif case == "offline":
        observation_id = _canonical(
            repository,
            suffix="quality:offline:event",
            event_at=assessed_at,
            payload=DeviceConnectivityPayload(
                state=DeviceConnectivityState.OFFLINE
            ),
        )
        _associate(episode_service, observation_id, assessed_at)
    elif case == "clock":
        observation_id = _canonical(
            repository,
            suffix="quality:clock:event",
            event_at=assessed_at - timedelta(seconds=30),
            payload=HeartRatePayload(value=65),
            quality_flags=("sensor_clock_uncertain",),
        )
        _associate(episode_service, observation_id, assessed_at)
    elif case == "missing":
        observation_id = _canonical(
            repository,
            suffix="quality:missing:event",
            event_at=START + timedelta(minutes=5),
            payload=MissingIntervalPayload(
                target_observation_type=ObservationType.HEART_RATE,
                missing_state=MissingState.MISSING,
                reason_code="stream_gap",
                interval_start_at=START + timedelta(minutes=4),
                interval_end_at=START + timedelta(minutes=5),
            ),
            missing_state=MissingState.MISSING,
        )
        _associate(
            episode_service,
            observation_id,
            START + timedelta(minutes=5),
        )

    result = _fast_path(
        repository,
        minimum_coverage_ratio=minimum,
        stale_after_seconds=stale_after,
        offline_after_seconds=offline_after,
        clock_invalid_flags=(
            ("sensor_clock_uncertain",) if case == "clock" else None
        ),
    ).evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id=f"evaluation:{case}",
        assessed_at=assessed_at,
    )
    assert result.quality.data_sufficiency == DataSufficiency.DATA_INSUFFICIENT
    assert expected_reason in result.quality.reason_codes
    assert result.current_risk.risk_state == RiskState.UNKNOWN
    assert (
        result.current_risk.data_sufficiency
        == DataSufficiency.DATA_INSUFFICIENT
    )
    assert result.current_risk.is_all_clear is False
    assert result.current_risk.health_escalation_allowed is False
    assert result.current_risk.policy_version == "risk-v1"
    assert result.current_risk.source_scope.night_episode_id == (
        episode.night_episode_id
    )
    assert result.current_risk.observed_at.tzinfo is not None
    connection.close()


def test_event_storm_hysteresis_cooldown_and_suppressed_receipts() -> None:
    repository, connection, _ = _repository()
    episode_service, episode = _open_episode(repository, suffix="storm")
    _fill_coverage(repository, episode_service)
    fast_path = _fast_path(
        repository,
        minimum_coverage_ratio=0,
        stale_after_seconds=10_000,
        offline_after_seconds=20_000,
    )
    first = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="storm:1",
        assessed_at=START + timedelta(minutes=10),
    )
    assert len(first.events) == 4
    assert all(
        receipt.outcome == FastPathReceiptOutcome.EMITTED
        for receipt in first.signal_receipts
    )

    repeated = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="storm:2",
        assessed_at=START + timedelta(minutes=10, seconds=1),
    )
    assert repeated.events == ()
    assert all(
        receipt.outcome == FastPathReceiptOutcome.SUPPRESSED_REPEAT
        and receipt.suppressed_repeat_count == 1
        for receipt in repeated.signal_receipts
    )

    for minute in range(11, 27):
        event_at = START + timedelta(minutes=minute)
        observation_id = _observation(
            repository,
            suffix=f"storm:continued:{minute}",
            event_at=event_at,
        )
        _associate(episode_service, observation_id, event_at)
    reminded = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="storm:3",
        assessed_at=START + timedelta(minutes=26),
    )
    assert len(reminded.events) == 4
    assert all(
        receipt.reason_code == "cooldown_reminder"
        for receipt in reminded.signal_receipts
    )

    out_at = START + timedelta(minutes=27)
    out_id = _canonical(
        repository,
        suffix="storm:out-of-bed",
        event_at=out_at,
        payload=BedPresencePayload(state=BedPresenceState.OUT_OF_BED),
    )
    _associate(episode_service, out_id, out_at)
    first_flip = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="storm:4",
        assessed_at=out_at,
    )
    bed_receipt = next(
        item
        for item in first_flip.signal_receipts
        if item.signal_type == FastPathSignalType.BED
    )
    assert bed_receipt.outcome == FastPathReceiptOutcome.SUPPRESSED_HYSTERESIS
    assert not any(
        event.attributes["signal_type"] == FastPathSignalType.BED.value
        for event in first_flip.events
    )
    second_flip = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="storm:5",
        assessed_at=out_at + timedelta(seconds=1),
    )
    bed_receipt = next(
        item
        for item in second_flip.signal_receipts
        if item.signal_type == FastPathSignalType.BED
    )
    assert bed_receipt.outcome == FastPathReceiptOutcome.EMITTED
    assert any(
        event.attributes["signal_type"] == FastPathSignalType.BED.value
        for event in second_flip.events
    )
    connection.close()


def test_alarm_stop_only_closes_exact_instance_and_unreviewed_is_operational() -> None:
    repository, connection, _ = _repository()
    episode_service, episode = _open_episode(repository, suffix="alerts")
    for minute, instance_id in ((1, "vendor-A"), (2, "vendor-B")):
        event_at = START + timedelta(minutes=minute)
        observation_id = _canonical(
            repository,
            suffix=f"alert:open:{instance_id}",
            event_at=event_at,
            payload=VendorAlertPayload(
                alert_code="UNREVIEWED_VENDOR_CODE",
                lifecycle_state=AlertLifecycleState.ACTIVE,
                vendor_alert_instance_id=instance_id,
            ),
        )
        _associate(episode_service, observation_id, event_at)
    orphan_id = _canonical(
        repository,
        suffix="alert:orphan-stop",
        event_at=START + timedelta(minutes=3),
        payload=VendorAlertPayload(
            alert_code="UNREVIEWED_VENDOR_CODE",
            lifecycle_state=AlertLifecycleState.STOPPED,
        ),
    )
    _associate(
        episode_service,
        orphan_id,
        START + timedelta(minutes=3),
    )
    fast_path = _fast_path(
        repository,
        minimum_coverage_ratio=0,
        stale_after_seconds=3600,
        offline_after_seconds=7200,
    )
    first = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="alerts:1",
        assessed_at=START + timedelta(minutes=3),
    )
    assert next(
        receipt
        for receipt in first.alert_receipts
        if receipt.observation_id == orphan_id
    ).outcome == AlertCorrelationOutcome.ORPHAN_STOP
    assert len(
        repository.list_vendor_alert_instances(
            NAMESPACE,
            night_episode_id=episode.night_episode_id,
            open_only=True,
        )
    ) == 2
    assert first.current_risk.risk_state == RiskState.OPERATIONAL_REVIEW
    assert "vendor_alert_signal_not_diagnosis" in (
        first.current_risk.reason_codes
    )
    assert first.current_risk.pending_domain_review_rule_ids
    assert first.current_risk.health_escalation_allowed is False

    exact_stop_id = _canonical(
        repository,
        suffix="alert:exact-stop-A",
        event_at=START + timedelta(minutes=4),
        payload=VendorAlertPayload(
            alert_code="UNREVIEWED_VENDOR_CODE",
            lifecycle_state=AlertLifecycleState.STOPPED,
            vendor_alert_instance_id="vendor-A",
        ),
    )
    _associate(
        episode_service,
        exact_stop_id,
        START + timedelta(minutes=4),
    )
    second = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="alerts:2",
        assessed_at=START + timedelta(minutes=4),
    )
    exact_receipt = next(
        receipt
        for receipt in second.alert_receipts
        if receipt.observation_id == exact_stop_id
    )
    assert (
        exact_receipt.outcome
        == AlertCorrelationOutcome.CLOSED_UNIQUE_INSTANCE
    )
    open_instances = repository.list_vendor_alert_instances(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        open_only=True,
    )
    assert [item.vendor_alert_instance_id for item in open_instances] == [
        "vendor-B"
    ]
    assert second.current_risk.risk_state == RiskState.OPERATIONAL_REVIEW
    connection.close()


def _care_command(
    *,
    episode,
    command_id: str,
    target: CareFollowupState,
    occurred_at: datetime,
) -> CareFollowupCommand:
    return CareFollowupCommand(
        command_id=command_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        night_episode_id=episode.night_episode_id,
        target_state=target,
        actor_id="care-operator",
        authorization_id="care-auth",
        reason_code=f"test:{target.value}",
        occurred_at=occurred_at,
        correlation_id=f"correlation:{command_id}",
    )


def test_service_mode_allows_active_tonight_and_yesterday_followup() -> None:
    repository, connection, _ = _repository()
    episode_service, yesterday = _open_episode(
        repository,
        suffix="coexist:yesterday",
    )
    episode_service.process_trigger(
        NAMESPACE,
        trigger=_command(
            trigger_id="coexist:stop-yesterday",
            kind=LifecycleTriggerKind.DEACTIVATE,
            occurred_at=START + timedelta(hours=8),
        ),
    )
    care = CareFollowupService(
        repository,
        access_policy=CareFollowupAccessPolicy(
            {"care-operator": frozenset({"care-auth"})}
        ),
    )
    pending = care.transition(
        NAMESPACE,
        _care_command(
            episode=yesterday,
            command_id="care:pending",
            target=CareFollowupState.PENDING_FEEDBACK,
            occurred_at=START + timedelta(hours=9),
        ),
    )
    assert pending.created

    tonight_at = START + timedelta(days=1)
    start_id = _observation(
        repository,
        suffix="coexist:tonight:start",
        event_at=tonight_at,
        bed_state=BedPresenceState.IN_BED,
    )
    tonight = episode_service.process_observation(
        NAMESPACE,
        observation_id=start_id,
        processed_at=tonight_at + timedelta(seconds=2),
    ).episode
    assert tonight is not None
    projector = ServiceModeProjector(repository)
    active = projector.project(
        NAMESPACE,
        subject_id="elder-1",
        projected_at=tonight_at + timedelta(minutes=1),
    )
    assert active.mode == ServiceMode.ACTIVE
    assert active.active_night_episode_id == tonight.night_episode_id
    assert active.open_followup_night_episode_ids == (
        yesterday.night_episode_id,
    )

    episode_service.process_trigger(
        NAMESPACE,
        trigger=_command(
            trigger_id="coexist:stop-tonight",
            kind=LifecycleTriggerKind.DEACTIVATE,
            occurred_at=tonight_at + timedelta(hours=8),
        ),
    )
    follow_up = projector.project(
        NAMESPACE,
        subject_id="elder-1",
        projected_at=tonight_at + timedelta(hours=8, minutes=1),
    )
    assert follow_up.mode == ServiceMode.FOLLOW_UP

    care.transition(
        NAMESPACE,
        _care_command(
            episode=yesterday,
            command_id="care:started",
            target=CareFollowupState.FOLLOWING_UP,
            occurred_at=tonight_at + timedelta(hours=9),
        ),
    )
    care.transition(
        NAMESPACE,
        _care_command(
            episode=yesterday,
            command_id="care:completed",
            target=CareFollowupState.COMPLETED,
            occurred_at=tonight_at + timedelta(hours=10),
        ),
    )
    dormant = projector.project(
        NAMESPACE,
        subject_id="elder-1",
        projected_at=tonight_at + timedelta(hours=10, minutes=1),
    )
    assert dormant.mode == ServiceMode.DORMANT
    assert "sleep_domain_service_mode" not in (
        POSTGRES_BASELINE_SQL
    )
    connection.close()


def test_fast_path_and_care_state_roll_back_when_outbox_insert_fails() -> None:
    repository, connection, _ = _repository()
    episode_service, episode = _open_episode(repository, suffix="atomic")
    before_outbox = repository.count_rows("sleep_domain_domain_outbox")
    connection.execute(
        """
        CREATE TRIGGER fail_domain_outbox
        BEFORE INSERT ON sleep_domain_domain_outbox
        BEGIN
          SELECT RAISE(ABORT, 'forced outbox failure');
        END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        _fast_path(
            repository,
            minimum_coverage_ratio=0,
            stale_after_seconds=3600,
            offline_after_seconds=7200,
        ).evaluate(
            NAMESPACE,
            night_episode_id=episode.night_episode_id,
            evaluation_id="atomic:fast-path",
            assessed_at=START + timedelta(minutes=1),
        )
    assert repository.count_rows("sleep_domain_quality_assessments") == 0
    assert repository.count_rows("sleep_domain_risk_assessments") == 0
    assert repository.count_rows(
        "sleep_domain_fast_path_signal_receipts"
    ) == 0
    assert repository.count_rows("sleep_domain_domain_outbox") == before_outbox

    care = CareFollowupService(
        repository,
        access_policy=CareFollowupAccessPolicy(
            {"care-operator": frozenset({"care-auth"})}
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        care.transition(
            NAMESPACE,
            _care_command(
                episode=episode,
                command_id="atomic:care",
                target=CareFollowupState.PENDING_FEEDBACK,
                occurred_at=START + timedelta(minutes=2),
            ),
        )
    assert repository.get_care_followup(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
    ) is None
    assert repository.count_rows(
        "sleep_domain_care_followup_transition_receipts"
    ) == 0
    connection.close()


def test_restart_recovers_risk_signal_projection_and_care_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deterministic-fast-path.sqlite3"
    encryption = _raw_policy()
    first_repository, first_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=encryption,
    )
    episode_service, episode = _open_episode(
        first_repository,
        suffix="restart",
    )
    fast_path = _fast_path(
        first_repository,
        minimum_coverage_ratio=0,
        stale_after_seconds=3600,
        offline_after_seconds=7200,
    )
    first = fast_path.evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="restart:1",
        assessed_at=START + timedelta(minutes=1),
    )
    for minute in (1, 2, 3):
        event_at = START + timedelta(minutes=minute)
        observation_id = _observation(
            first_repository,
            suffix=f"restart:continued:{minute}",
            event_at=event_at,
        )
        _associate(episode_service, observation_id, event_at)
    care = CareFollowupService(
        first_repository,
        access_policy=CareFollowupAccessPolicy(
            {"care-operator": frozenset({"care-auth"})}
        ),
    )
    care.transition(
        NAMESPACE,
        _care_command(
            episode=episode,
            command_id="restart:care",
            target=CareFollowupState.PENDING_FEEDBACK,
            occurred_at=START + timedelta(minutes=2),
        ),
    )
    first_connection.close()

    second_repository, second_connection, _ = _repository(
        sqlite3.connect(database, check_same_thread=False),
        policy=encryption,
    )
    assert second_repository.get_current_quality(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
    ) == first.quality
    assert second_repository.get_current_risk(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
    ) == first.current_risk
    for signal_type in FastPathSignalType:
        assert second_repository.get_fast_path_signal_projection(
            NAMESPACE,
            night_episode_id=episode.night_episode_id,
            signal_type=signal_type.value,
        ) is not None
    restored = second_repository.get_care_followup(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
    )
    assert restored is not None
    assert restored.state == CareFollowupState.PENDING_FEEDBACK

    repeated = _fast_path(
        second_repository,
        minimum_coverage_ratio=0,
        stale_after_seconds=3600,
        offline_after_seconds=7200,
    ).evaluate(
        NAMESPACE,
        night_episode_id=episode.night_episode_id,
        evaluation_id="restart:2",
        assessed_at=START + timedelta(minutes=3),
    )
    assert repeated.events == ()
    assert all(
        receipt.outcome == FastPathReceiptOutcome.SUPPRESSED_REPEAT
        and receipt.suppressed_repeat_count == 1
        for receipt in repeated.signal_receipts
    )
    second_connection.close()


def test_fast_path_migration_has_no_agent_threshold_or_service_mode_state() -> None:
    assert SCHEMA_VERSION == "001_initial_schema"
    sql = POSTGRES_BASELINE_SQL
    for table in (
        "sleep_domain_quality_assessments",
        "sleep_domain_current_quality",
        "sleep_domain_risk_assessments",
        "sleep_domain_current_risk",
        "sleep_domain_vendor_alert_instances",
        "sleep_domain_fast_path_signal_receipts",
        "sleep_domain_care_followups",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    lowered = sql.lower()
    assert "service_mode" not in lowered
    assert "heart_rate" not in lowered
    assert "respiratory_rate" not in lowered
    assert "medical_threshold" not in lowered


@pytest.mark.parametrize(
    ("risk_state", "health_escalation_allowed"),
    [
        (RiskState.REVIEWED_SIGNAL, False),
        (RiskState.OPERATIONAL_REVIEW, True),
    ],
)
def test_pending_domain_review_rule_cannot_health_escalate(
    risk_state: RiskState,
    health_escalation_allowed: bool,
) -> None:
    with pytest.raises(ValueError, match="PENDING_DOMAIN_REVIEW"):
        ReviewedVendorAlertRule(
            rule_id="pending-rule",
            rule_version="pending-rule-v1",
            provider_id="perceptor",
            alert_code="UNREVIEWED_VENDOR_CODE",
            review_status=DomainRuleReviewStatus.PENDING_DOMAIN_REVIEW,
            risk_state=risk_state,
            health_escalation_allowed=health_escalation_allowed,
        )
