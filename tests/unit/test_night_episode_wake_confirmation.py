from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    ConfidenceValue,
    DataMode,
    DeterministicQualityPolicy,
    DeterministicRiskPolicy,
    EpisodeBoundaryPolicy,
    FastPathEventPolicy,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    SleepObservation,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.domain.episodes import NightEpisodeV2, uuid7_from_parts
from sleepagent.infrastructure.postgres_sleep_slice import (
    ClosedEpisodeCandidate,
    EpisodeLifecycleProjector,
    EpisodeProjectionBoundary,
    EpisodeRevisionMutation,
    LifecycleSnapshotRecord,
    SleepSlicePolicy,
    StoredEpisode,
)
from sleepagent.persistence.uow import UowScope


UTC = timezone.utc
ZONE = ZoneInfo("Asia/Shanghai")


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, now: datetime | None = None) -> str:
        self.value += 1
        instant = now or datetime(2026, 1, 1, tzinfo=UTC)
        return uuid7_from_parts(
            int(instant.astimezone(UTC).timestamp() * 1000), self.value
        )


def _policy(*, confirmation_seconds: int = 30 * 60) -> SleepSlicePolicy:
    return SleepSlicePolicy(
        boundary=EpisodeBoundaryPolicy(
            policy_version="boundary-test-wake-confirmation",
            rollover_local_minute=12 * 60,
            report_deadline_local_minute=10 * 60,
            maximum_episode_seconds=20 * 3600,
            wake_confirmation_seconds=confirmation_seconds,
            allowed_lateness_seconds=2 * 3600,
        ),
        quality=DeterministicQualityPolicy(
            policy_version="quality-test",
            minimum_coverage_ratio=0,
            partial_coverage_ratio=0,
            stale_after_seconds=86_400,
            offline_after_seconds=172_800,
        ),
        risk=DeterministicRiskPolicy(policy_version="risk-test"),
        events=FastPathEventPolicy(policy_version="event-test"),
    )


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:wake-confirmation",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-1",
        arm_id="arm-1",
        process_role="worker",
        purpose="durable_work",
        service_principal_id="worker-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(state=AvailabilityState.KNOWN, value=1),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.KNOWN, value="test"
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
        completeness=1,
    )


def _observation(
    ids: Ids,
    state: BedPresenceState,
    event_at: datetime,
    *,
    received_at: datetime | None = None,
) -> SleepObservation:
    observation_id = ids(event_at)
    return SleepObservation(
        observation_id=observation_id,
        data_mode=DataMode.REPLAY,
        observation_type=ObservationType.BED_PRESENCE,
        payload=BedPresencePayload(state=state),
        subject_id="subject-1",
        device_id="device-1",
        device_binding_id="binding-1",
        binding_version=1,
        event_occurred_at=event_at,
        received_at=received_at or event_at + timedelta(seconds=1),
        timezone_status=TimezoneStatus.KNOWN,
        source_kind=SourceKind.DEVICE_MEASURED,
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="provider-test",
            provider_account_id="account-test",
            adapter_id="adapter-test",
            adapter_version="1",
            raw_ingress_record_id=f"raw:{observation_id}",
            raw_payload_sha256="a" * 64,
        ),
        source_key=f"source:{observation_id}",
        idempotency_key=f"identity:{observation_id}",
    )


def _snapshot(
    mutation: EpisodeRevisionMutation,
    *,
    cas_version: int,
) -> LifecycleSnapshotRecord:
    return LifecycleSnapshotRecord(
        monitoring_snapshot_id="snapshot-1",
        state="active",
        cas_version=cas_version,
        created_at=mutation.episode.created_at,
        updated_at=mutation.episode.updated_at,
        episode=StoredEpisode(
            episode=mutation.episode,
            database_state=mutation.database_state,
            current_revision_id=mutation.revision_id,
            observation_ids=mutation.observation_ids,
            cas_version=cas_version,
        ),
    )


def _project(
    projector: EpisodeLifecycleProjector,
    ids: Ids,
    snapshot: LifecycleSnapshotRecord,
    state: BedPresenceState,
    event_at: datetime,
    *,
    committed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> EpisodeRevisionMutation:
    observation = _observation(
        ids, state, event_at, received_at=received_at
    )
    mutation = projector.project(
        scope=_scope(),
        snapshot=snapshot,
        observation=observation,
        opening_identity=observation.idempotency_key,
        timezone_name="Asia/Shanghai",
        committed_at=committed_at or observation.received_at,
        conflicting_episode=lambda _value: None,
    )
    assert mutation is not None
    return mutation


def _open(
    projector: EpisodeLifecycleProjector, ids: Ids, bed_at: datetime
) -> EpisodeRevisionMutation:
    return _project(
        projector,
        ids,
        LifecycleSnapshotRecord(None, "dormant", 0, None, None),
        BedPresenceState.IN_BED,
        bed_at,
    )


def test_real_night_transient_exits_then_scheduled_wake_survives_restart() -> None:
    ids = Ids()
    policy = _policy()
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    bed_at = datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    opened = _open(projector, ids, bed_at)
    episode_id = opened.episode.night_episode_id

    transient_out = _project(
        projector,
        ids,
        _snapshot(opened, cas_version=1),
        BedPresenceState.OUT_OF_BED,
        datetime(2026, 9, 8, 0, 16, tzinfo=ZONE),
    )
    assert transient_out.database_state == "collecting"
    assert transient_out.closes_episode is False
    assert transient_out.episode.wake_at is None
    assert transient_out.episode.candidate_wake_at == datetime(
        2026, 9, 8, 0, 16, tzinfo=ZONE
    )

    returned = _project(
        projector,
        ids,
        _snapshot(transient_out, cas_version=2),
        BedPresenceState.IN_BED,
        datetime(2026, 9, 8, 0, 20, tzinfo=ZONE),
        committed_at=datetime(2026, 9, 8, 3, 0, tzinfo=ZONE),
    )
    assert returned.episode.night_episode_id == episode_id
    assert returned.episode.candidate_wake_at is None
    assert returned.episode.wake_at is None
    assert returned.revision_cause == "wake_candidate_cancelled"
    assert transient_out.observation_ids[-1] in returned.observation_ids

    short_out = _project(
        projector,
        ids,
        _snapshot(returned, cas_version=3),
        BedPresenceState.OUT_OF_BED,
        datetime(2026, 9, 8, 2, 10, tzinfo=ZONE),
    )
    short_in = _project(
        projector,
        ids,
        _snapshot(short_out, cas_version=4),
        BedPresenceState.IN_BED,
        datetime(2026, 9, 8, 2, 18, tzinfo=ZONE),
    )
    final_out_at = datetime(2026, 9, 8, 6, 55, tzinfo=ZONE)
    final_out = _project(
        projector,
        ids,
        _snapshot(short_in, cas_version=5),
        BedPresenceState.OUT_OF_BED,
        final_out_at,
    )
    assert final_out.episode.night_episode_id == episode_id
    assert final_out.episode.current_revision == 6

    # Reconstruct the aggregate from its durable JSON, as a restarted worker does.
    durable_episode = NightEpisodeV2.model_validate(
        final_out.episode.model_dump(mode="json")
    )
    restarted_snapshot = _snapshot(final_out, cas_version=6)
    restarted_snapshot = LifecycleSnapshotRecord(
        restarted_snapshot.monitoring_snapshot_id,
        restarted_snapshot.state,
        restarted_snapshot.cas_version,
        restarted_snapshot.created_at,
        restarted_snapshot.updated_at,
        StoredEpisode(
            durable_episode,
            "collecting",
            final_out.revision_id,
            final_out.observation_ids,
            6,
        ),
    )

    class Repository:
        def lock_subject_lifecycle(self) -> None:
            return None

        def load_lifecycle(self) -> LifecycleSnapshotRecord:
            return restarted_snapshot

        def find_conflicting_episode(self, **_kwargs: object) -> None:
            return None

    confirmation_at = final_out_at + timedelta(
        seconds=policy.boundary.wake_confirmation_seconds
    )
    decision = EpisodeProjectionBoundary(
        policy, id_generator=ids
    ).prepare_scheduled_close(
        scope=_scope(),
        repository=Repository(),  # type: ignore[arg-type]
        committed_at=confirmation_at,
    )
    assert decision is not None and decision.mutation is not None
    confirmed = decision.mutation
    assert confirmed.episode.night_episode_id == episode_id
    assert confirmed.database_state == "awaiting_report"
    assert confirmed.closes_episode is True
    assert confirmed.episode.wake_at == final_out_at
    assert confirmed.episode.candidate_wake_at is None
    assert confirmed.revision_cause == "confirmed_observed_wake"


def test_repeated_out_keeps_candidate_anchor_and_can_confirm_from_event_time() -> None:
    ids = Ids()
    policy = _policy(confirmation_seconds=60)
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    bed_at = datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    opened = _open(projector, ids, bed_at)
    wake_at = datetime(2026, 9, 8, 6, 55, tzinfo=ZONE)
    first = _project(
        projector,
        ids,
        _snapshot(opened, cas_version=1),
        BedPresenceState.OUT_OF_BED,
        wake_at,
    )
    second = _project(
        projector,
        ids,
        _snapshot(first, cas_version=2),
        BedPresenceState.OUT_OF_BED,
        wake_at + timedelta(seconds=30),
    )
    assert second.episode.candidate_wake_at == wake_at
    confirmed = _project(
        projector,
        ids,
        _snapshot(second, cas_version=3),
        BedPresenceState.OUT_OF_BED,
        wake_at + timedelta(seconds=60),
    )
    assert confirmed.closes_episode is True
    assert confirmed.episode.wake_at == wake_at
    assert confirmed.revision_cause == "confirmed_observed_wake"


def test_return_immediately_before_threshold_cancels_candidate() -> None:
    ids = Ids()
    policy = _policy(confirmation_seconds=60)
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    opened = _open(
        projector, ids, datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    )
    out_at = datetime(2026, 9, 8, 1, 0, tzinfo=ZONE)
    candidate = _project(
        projector,
        ids,
        _snapshot(opened, cas_version=1),
        BedPresenceState.OUT_OF_BED,
        out_at,
    )
    returned = _project(
        projector,
        ids,
        _snapshot(candidate, cas_version=2),
        BedPresenceState.IN_BED,
        out_at + timedelta(seconds=59, microseconds=999_999),
    )
    assert returned.closes_episode is False
    assert returned.episode.wake_at is None
    assert returned.episode.candidate_wake_at is None
    assert returned.episode.night_episode_id == opened.episode.night_episode_id


def test_late_history_uses_event_order_and_stale_out_cannot_restart_candidate() -> None:
    ids = Ids()
    projector = EpisodeLifecycleProjector(_policy(), id_generator=ids)
    opened = _open(
        projector, ids, datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    )
    out_at = datetime(2026, 9, 8, 0, 16, tzinfo=ZONE)
    candidate = _project(
        projector,
        ids,
        _snapshot(opened, cas_version=1),
        BedPresenceState.OUT_OF_BED,
        out_at,
    )
    returned = _project(
        projector,
        ids,
        _snapshot(candidate, cas_version=2),
        BedPresenceState.IN_BED,
        out_at + timedelta(minutes=4),
        committed_at=out_at + timedelta(hours=4),
        received_at=out_at + timedelta(hours=4),
    )
    stale_out = _project(
        projector,
        ids,
        _snapshot(returned, cas_version=3),
        BedPresenceState.OUT_OF_BED,
        out_at + timedelta(minutes=2),
        committed_at=out_at + timedelta(hours=5),
        received_at=out_at + timedelta(hours=5),
    )
    assert stale_out.revision_cause == "normalized_observation_stale_event"
    assert stale_out.episode.candidate_wake_at is None
    assert stale_out.episode.latest_bed_presence_at == out_at + timedelta(minutes=4)
    assert stale_out.observation_ids != returned.observation_ids


def test_deadline_fallback_still_closes_without_confirmed_wake() -> None:
    ids = Ids()
    policy = _policy()
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    opened = _open(
        projector, ids, datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    )
    closed = projector.close_at_deadline(
        snapshot=_snapshot(opened, cas_version=1),
        committed_at=opened.episode.deterministic_close_deadline_at,
        conflicting_episode=lambda _value: None,
    )
    assert closed.closes_episode is True
    assert closed.database_state == "awaiting_report"
    assert closed.episode.wake_at is None
    assert closed.revision_cause == "deadline_fallback"


def test_return_after_confirmed_wake_is_fail_closed_not_a_second_episode() -> None:
    ids = Ids()
    policy = _policy(confirmation_seconds=60)
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    bed_at = datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    opened = _open(projector, ids, bed_at)
    wake_at = datetime(2026, 9, 8, 6, 55, tzinfo=ZONE)
    candidate = _project(
        projector,
        ids,
        _snapshot(opened, cas_version=1),
        BedPresenceState.OUT_OF_BED,
        wake_at,
    )
    confirmed = projector.close_at_confirmed_wake(
        snapshot=_snapshot(candidate, cas_version=2),
        confirmed_at=wake_at + timedelta(seconds=60),
        committed_at=wake_at + timedelta(seconds=60),
        conflicting_episode=lambda _value: None,
    )
    return_observation = _observation(
        ids,
        BedPresenceState.IN_BED,
        wake_at + timedelta(seconds=61),
    )
    closed_candidate = ClosedEpisodeCandidate(
        stored_episode=StoredEpisode(
            confirmed.episode,
            confirmed.database_state,
            confirmed.revision_id,
            confirmed.observation_ids,
            3,
        ),
        window_end_at=wake_at,
        contains_observation_time=False,
    )

    class Repository:
        def lock_subject_lifecycle(self) -> None:
            return None

        def load_lifecycle(self) -> LifecycleSnapshotRecord:
            return LifecycleSnapshotRecord(
                "snapshot-1", "dormant", 3, bed_at, wake_at, None
            )

        def has_episode_membership(self, _observation_id: str) -> bool:
            return False

        def load_closed_episode_candidates(
            self, **_kwargs: object
        ) -> tuple[ClosedEpisodeCandidate, ...]:
            return (closed_candidate,)

    decision = EpisodeProjectionBoundary(policy, id_generator=ids).prepare(
        scope=_scope(),
        repository=Repository(),  # type: ignore[arg-type]
        observation=return_observation,
        opening_identity=return_observation.idempotency_key,
        timezone_name="Asia/Shanghai",
        committed_at=return_observation.received_at,
        canonical_is_persisted=True,
    )
    assert decision.mutation is None
    assert decision.late_association is not None
    assert decision.late_association.status == "no_match"
    assert decision.late_association.reason_code == "LATE_ASSOCIATION_NO_MATCH"


def test_duplicate_membership_is_projection_idempotent() -> None:
    ids = Ids()
    policy = _policy()
    projector = EpisodeLifecycleProjector(policy, id_generator=ids)
    opened = _open(
        projector, ids, datetime(2026, 9, 7, 23, 50, tzinfo=ZONE)
    )
    observation = _observation(
        ids,
        BedPresenceState.OUT_OF_BED,
        datetime(2026, 9, 8, 0, 16, tzinfo=ZONE),
    )

    class Repository:
        def lock_subject_lifecycle(self) -> None:
            return None

        def load_lifecycle(self) -> LifecycleSnapshotRecord:
            return _snapshot(opened, cas_version=1)

        def has_episode_membership(self, _observation_id: str) -> bool:
            return True

    decision = EpisodeProjectionBoundary(policy, id_generator=ids).prepare(
        scope=_scope(),
        repository=Repository(),  # type: ignore[arg-type]
        observation=observation,
        opening_identity=observation.idempotency_key,
        timezone_name="Asia/Shanghai",
        committed_at=observation.received_at,
        canonical_is_persisted=True,
    )
    assert decision.mutation is None
    assert decision.persist_required is False
