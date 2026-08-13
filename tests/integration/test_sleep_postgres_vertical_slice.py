from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from sleepagent.api.postgres import ResolvedActorAuthority
from sleepagent.api.product_contracts import ProductRole
from sleepagent.persistence.uow import UowScope
from sleepagent.api.public_auth import (
    ActorAssertionClaims,
    AuthoritativeRoleBinding,
    ServicePrincipal,
)
from sleepagent.api.public_contracts import (
    PublicActorRole,
    PublicErrorCode,
    SleepApiApplicationError,
)
from sleepagent.api.public_runtime import (
    PostgresAuthenticatedActorContext,
    PostgresSleepApiRuntime,
)
from sleepagent.domain.contracts import (
    AlertLifecycleState,
    AlgorithmVersionValue,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    CollectionWindowDerivation,
    ConfidenceValue,
    CurrentRisk,
    DataMode,
    DataSufficiency,
    DeterministicQualityPolicy,
    DeterministicRiskPolicy,
    DeviceBindingReference,
    DomainRuleReviewStatus,
    EpisodeBoundaryPolicy,
    FastPathEventPolicy,
    MissingState,
    NightEpisode,
    NightEpisodeState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ReviewedVendorAlertRule,
    RiskState,
    SleepObservation,
    SourceKind,
    CurrentRevisionPointer,
    SubjectLifecycleLease,
    TimezoneStatus,
    VendorAlertPayload,
)
from sleepagent.domain.episodes import (
    EpisodeAssignmentBasis,
    EpisodePublicationStatus,
    uuid7_from_parts,
)
from sleepagent.domain.postgres_slice import (
    EpisodeLifecycleProjector,
    FastPathHandler,
    FastPathLease,
    IngressResult,
    LifecycleSnapshotRecord,
    LoadedFastPathOperation,
    PostgresSleepSliceRepository,
    RawPayloadCipher,
    ReplayIngressHandler,
    ReplayObservationInput,
    ReplayRawBatch,
    SleepSliceInvariantError,
    SleepSlicePolicy,
    SleepSliceStaleRevision,
    StoredEpisode,
)


UTC = timezone.utc


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, now: datetime | None = None) -> str:
        self.value += 1
        instant = now or datetime(2026, 1, 1, tzinfo=UTC)
        milliseconds = int(instant.astimezone(UTC).timestamp() * 1000)
        return uuid7_from_parts(milliseconds, self.value)


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(
            state=AvailabilityState.KNOWN,
            value=1.0,
        ),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.KNOWN,
            value="replay-v1",
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
        completeness=1.0,
    )


def _input(
    *,
    state: BedPresenceState,
    at: datetime,
    identity: str,
) -> ReplayObservationInput:
    return ReplayObservationInput(
        provider_id="replay-provider",
        provider_account_id="replay-account",
        provider_device_id="provider-device-opaque",
        subject_id="subject-1",
        device_id="device-1",
        device_binding_id="binding-1",
        binding_version=1,
        timezone_name="Asia/Shanghai",
        observation_type=ObservationType.BED_PRESENCE,
        payload=BedPresencePayload(state=state),
        source_kind=SourceKind.DEVICE_MEASURED,
        quality=_quality(),
        event_occurred_at=at,
        received_at=at + timedelta(seconds=1),
        timezone_status=TimezoneStatus.KNOWN,
        source_key=f"source:{identity}",
        idempotency_identity=identity,
    )


def _observation(
    source: ReplayObservationInput,
    *,
    observation_id: str,
) -> SleepObservation:
    return SleepObservation(
        observation_id=observation_id,
        data_mode=DataMode.REPLAY,
        observation_type=source.observation_type,
        payload=source.payload,
        subject_id=source.subject_id,
        device_id=source.device_id,
        device_binding_id=source.device_binding_id,
        binding_version=source.binding_version,
        event_occurred_at=source.event_occurred_at,
        received_at=source.received_at,
        timezone_status=source.timezone_status,
        source_kind=source.source_kind,
        quality=source.quality,
        provenance=ObservationProvenance(
            provider_id=source.provider_id,
            provider_account_id=source.provider_account_id,
            adapter_id="replay-observation-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=f"raw:{observation_id}",
            raw_payload_sha256="a" * 64,
        ),
        source_key=source.source_key,
        idempotency_key=source.idempotency_identity,
    )


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:pytest",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-1",
        arm_id="arm-1",
        process_role="worker",
        purpose="durable_work",
        service_principal_id="sleepagent-worker-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def _policy(*, urgent: bool = False) -> SleepSlicePolicy:
    rules = ()
    if urgent:
        rules = (
            ReviewedVendorAlertRule(
                rule_id="reviewed-fall-rule",
                rule_version="1",
                provider_id="replay-provider",
                alert_code="possible_fall",
                review_status=DomainRuleReviewStatus.APPROVED,
                risk_state=RiskState.REVIEWED_SIGNAL,
                health_escalation_allowed=True,
                evidence_references=("clinical-review:1",),
                reviewed_by_actor_id="reviewer-1",
                reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    return SleepSlicePolicy(
        boundary=EpisodeBoundaryPolicy(
            policy_version="boundary-v1",
            rollover_local_minute=12 * 60,
            report_deadline_local_minute=10 * 60,
            maximum_episode_seconds=20 * 3600,
            allowed_lateness_seconds=2 * 3600,
        ),
        quality=DeterministicQualityPolicy(
            policy_version="quality-v1",
            minimum_coverage_ratio=0,
            partial_coverage_ratio=0,
            stale_after_seconds=86_400,
            offline_after_seconds=172_800,
        ),
        risk=DeterministicRiskPolicy(
            policy_version="risk-v1",
            vendor_alert_rules=rules,
        ),
        events=FastPathEventPolicy(
            policy_version="fast-path-event-v1",
            cooldown_seconds=0,
        ),
    )


def test_episode_projector_finalizes_on_observed_wake_date() -> None:
    ids = Ids()
    projector = EpisodeLifecycleProjector(_policy(), id_generator=ids)
    zone = ZoneInfo("Asia/Shanghai")
    bed_at = datetime(2026, 8, 7, 23, 30, tzinfo=zone)
    wake_at = datetime(2026, 8, 8, 7, 5, tzinfo=zone)
    opened = projector.project(
        scope=_scope(),
        snapshot=LifecycleSnapshotRecord(
            monitoring_snapshot_id=None,
            state="dormant",
            cas_version=0,
            created_at=None,
            updated_at=None,
        ),
        observation=_observation(
            _input(state=BedPresenceState.IN_BED, at=bed_at, identity="bed-in"),
            observation_id=ids(bed_at),
        ),
        opening_identity="bed-in",
        timezone_name="Asia/Shanghai",
        committed_at=bed_at + timedelta(seconds=2),
        conflicting_episode=lambda _value: None,
    )
    assert opened is not None
    assert opened.episode.publication_status == EpisodePublicationStatus.PROVISIONAL
    assert opened.episode.episode_local_date is None
    assert opened.episode.legacy_local_sleep_date == date(2026, 8, 7)

    closed = projector.project(
        scope=_scope(),
        snapshot=LifecycleSnapshotRecord(
            monitoring_snapshot_id=ids(bed_at),
            state="active",
            cas_version=1,
            created_at=bed_at,
            updated_at=bed_at,
            episode=StoredEpisode(
                episode=opened.episode,
                database_state="collecting",
                current_revision_id=opened.revision_id,
                observation_ids=opened.observation_ids,
                cas_version=1,
            ),
        ),
        observation=_observation(
            _input(state=BedPresenceState.OUT_OF_BED, at=wake_at, identity="wake"),
            observation_id=ids(wake_at),
        ),
        opening_identity="wake",
        timezone_name="Asia/Shanghai",
        committed_at=wake_at + timedelta(seconds=2),
        conflicting_episode=lambda _value: None,
    )
    assert closed is not None
    assert closed.episode.episode_local_date == date(2026, 8, 8)
    assert closed.episode.assignment_basis == EpisodeAssignmentBasis.OBSERVED_WAKE
    assert closed.episode.current_revision == 2
    assert closed.enqueues_fast_path is True
    assert closed.promotes_revision is True
    assert closed.domain_event_type == "NIGHT_EPISODE_REVISION_COMMITTED"


def test_episode_date_conflict_is_unpublishable_and_does_not_enqueue_fast_path() -> None:
    ids = Ids()
    projector = EpisodeLifecycleProjector(_policy(), id_generator=ids)
    zone = ZoneInfo("Asia/Shanghai")
    bed_at = datetime(2026, 8, 7, 23, 30, tzinfo=zone)
    opened = projector.project(
        scope=_scope(),
        snapshot=LifecycleSnapshotRecord(None, "dormant", 0, None, None),
        observation=_observation(
            _input(state=BedPresenceState.IN_BED, at=bed_at, identity="bed-in"),
            observation_id=ids(bed_at),
        ),
        opening_identity="bed-in",
        timezone_name="Asia/Shanghai",
        committed_at=bed_at + timedelta(seconds=2),
        conflicting_episode=lambda _value: None,
    )
    assert opened is not None
    wake_at = datetime(2026, 8, 8, 7, 5, tzinfo=zone)
    closed = projector.project(
        scope=_scope(),
        snapshot=LifecycleSnapshotRecord(
            ids(bed_at),
            "active",
            1,
            bed_at,
            bed_at,
            StoredEpisode(
                opened.episode,
                "collecting",
                opened.revision_id,
                opened.observation_ids,
                1,
            ),
        ),
        observation=_observation(
            _input(state=BedPresenceState.OUT_OF_BED, at=wake_at, identity="wake"),
            observation_id=ids(wake_at),
        ),
        opening_identity="wake",
        timezone_name="Asia/Shanghai",
        committed_at=wake_at + timedelta(seconds=2),
        conflicting_episode=lambda _value: "existing-episode",
    )
    assert closed is not None
    assert closed.episode.publication_status == (
        EpisodePublicationStatus.RECONCILIATION_REQUIRED
    )
    assert closed.conflicting_episode_id == "existing-episode"
    assert closed.enqueues_fast_path is False
    assert closed.promotes_revision is False
    assert closed.revision_cause == "observed_wake_date_conflict"
    assert len(closed.observation_ids) == 2
    assert len(closed.new_membership_observation_ids) == 1
    assert (
        closed.domain_event_type
        == "NIGHT_EPISODE_DATE_RECONCILIATION_REQUIRED"
    )

    class RecordingCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.statements: list[tuple[str, Any]] = []
            self._row: tuple[bool] | None = None

        def execute(self, query: str, params: Any = None) -> None:
            self.statements.append((query, params))
            if "count(*) = cardinality" in query:
                self._row = (True,)

        def fetchone(self) -> tuple[bool] | None:
            return self._row

    cursor = RecordingCursor()
    repository = PostgresSleepSliceRepository(
        object(),  # type: ignore[arg-type]
        _scope(),
        id_generator=ids,
    )
    repository._write_episode_mutation(
        cursor,
        closed,
        _policy(),
        wake_at + timedelta(seconds=2),
    )
    aggregate_update = cursor.statements[0][0]
    set_clause = aggregate_update.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "current_revision_id" not in set_clause
    assert "current_revision_number" not in set_clause
    assert "episode_local_date" not in set_clause
    assert "date_state" not in set_clause
    assert "date_conflict" not in set_clause
    assert "current_revision_id = %s" in aggregate_update
    assert "current_revision_number = %s" in aggregate_update
    membership_insert, membership_insert_params = next(
        (statement, params)
        for statement, params in cursor.statements
        if "INSERT INTO public.sleep_domain_episode_observation_memberships"
        in statement
    )
    assert "'episode-membership:'" in membership_insert
    assert "decode('00', 'hex')" in membership_insert
    assert "ON CONFLICT (namespace_id, data_mode, observation_id) DO NOTHING" in (
        membership_insert
    )
    assert membership_insert_params[-1] == list(
        closed.new_membership_observation_ids
    )
    membership_check, membership_check_params = next(
        (statement, params)
        for statement, params in cursor.statements
        if "count(*) = cardinality" in statement
    )
    assert membership_check_params[0] == list(
        closed.new_membership_observation_ids
    )
    assert membership_check_params[-1] == list(
        closed.new_membership_observation_ids
    )
    assert "array_agg" not in membership_check
    assert "membership.night_episode_id = %s" in membership_check
    assert all(
        "array_agg(membership.observation_id" not in statement
        for statement, _params in cursor.statements
    )


def test_episode_without_wake_closes_on_estimated_deadline_date() -> None:
    ids = Ids()
    projector = EpisodeLifecycleProjector(_policy(), id_generator=ids)
    zone = ZoneInfo("Asia/Shanghai")
    bed_at = datetime(2026, 8, 7, 23, 30, tzinfo=zone)
    opened = projector.project(
        scope=_scope(),
        snapshot=LifecycleSnapshotRecord(None, "dormant", 0, None, None),
        observation=_observation(
            _input(state=BedPresenceState.IN_BED, at=bed_at, identity="bed-in"),
            observation_id=ids(bed_at),
        ),
        opening_identity="bed-in",
        timezone_name="Asia/Shanghai",
        committed_at=bed_at + timedelta(seconds=2),
        conflicting_episode=lambda _value: None,
    )
    assert opened is not None
    snapshot = LifecycleSnapshotRecord(
        monitoring_snapshot_id=ids(bed_at),
        state="active",
        cas_version=1,
        created_at=bed_at,
        updated_at=bed_at,
        episode=StoredEpisode(
            episode=opened.episode,
            database_state="collecting",
            current_revision_id=opened.revision_id,
            observation_ids=opened.observation_ids,
            cas_version=1,
        ),
    )
    deadline = opened.episode.deterministic_close_deadline_at

    closed = projector.close_at_deadline(
        snapshot=snapshot,
        committed_at=deadline,
        conflicting_episode=lambda _value: None,
    )

    assert closed.episode.assignment_basis == (
        EpisodeAssignmentBasis.DEADLINE_FALLBACK
    )
    assert closed.episode.episode_local_date == date(2026, 8, 8)
    assert closed.episode.wake_at is None
    assert closed.episode.publication_status == EpisodePublicationStatus.COMMITTED
    assert closed.revision_cause == "deadline_fallback"
    assert closed.enqueues_fast_path is True
    assert closed.new_membership_observation_ids == ()

    class RecordingCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, query: str, _params: Any = None) -> None:
            self.statements.append(query)

    cursor = RecordingCursor()
    PostgresSleepSliceRepository(
        object(),  # type: ignore[arg-type]
        _scope(),
        id_generator=ids,
    )._write_episode_mutation(
        cursor,
        closed,
        _policy(),
        deadline,
    )
    assert any(
        "INSERT INTO public.sleep_domain_night_episode_revisions" in statement
        for statement in cursor.statements
    )
    assert all(
        "sleep_domain_episode_observation_memberships" not in statement
        for statement in cursor.statements
    )

    with pytest.raises(
        SleepSliceInvariantError,
        match="before its deterministic deadline",
    ):
        projector.close_at_deadline(
            snapshot=snapshot,
            committed_at=deadline - timedelta(microseconds=1),
            conflicting_episode=lambda _value: None,
        )


@pytest.mark.parametrize(
    ("opened_at", "deadline_minute", "expected_message"),
    [
        (
            datetime(2026, 3, 7, 23, 0, tzinfo=ZoneInfo("America/New_York")),
            150,
            "nonexistent local-time gap",
        ),
        (
            datetime(2026, 10, 31, 23, 0, tzinfo=ZoneInfo("America/New_York")),
            90,
            "ambiguous without an explicit DST fold",
        ),
    ],
)
def test_deadline_policy_fails_closed_for_dst_gap_or_ambiguous_fold(
    opened_at: datetime,
    deadline_minute: int,
    expected_message: str,
) -> None:
    base = _policy()
    policy = SleepSlicePolicy(
        boundary=EpisodeBoundaryPolicy(
            policy_version="dst-boundary-v1",
            rollover_local_minute=12 * 60,
            report_deadline_local_minute=deadline_minute,
            maximum_episode_seconds=20 * 3600,
            allowed_lateness_seconds=2 * 3600,
        ),
        quality=base.quality,
        risk=base.risk,
        events=base.events,
    )

    with pytest.raises(SleepSliceInvariantError, match=expected_message):
        policy.deterministic_deadline(opened_at, "America/New_York")


def test_replay_batch_is_bounded_before_any_database_work() -> None:
    instant = datetime(2026, 8, 7, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    items = tuple(
        _input(
            state=BedPresenceState.IN_BED,
            at=instant,
            identity=f"item-{index}",
        )
        for index in range(101)
    )
    with pytest.raises(ValidationError, match="at most 100"):
        ReplayRawBatch(items=items)


class _FakeUow:
    def __init__(self) -> None:
        self.connection = object()
        self.commits = 0

    def __enter__(self) -> "_FakeUow":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1


class _FakeUowFactory:
    def __init__(self, uow: _FakeUow) -> None:
        self.uow = uow
        self.scopes: list[UowScope] = []

    def begin(self, scope: UowScope) -> _FakeUow:
        self.scopes.append(scope)
        return self.uow


class _IngressRepository:
    def __init__(self) -> None:
        self.prepared = []

    def ingest_raw(self, prepared: Any) -> IngressResult:
        self.prepared.append(prepared)
        return IngressResult(
            prepared.raw_ingress_record_id,
            prepared.normalization_work_id,
            prepared.intake_receipt_id,
            False,
        )


def test_raw_batch_commits_one_uow_and_keeps_payload_encrypted() -> None:
    uow = _FakeUow()
    factory = _FakeUowFactory(uow)
    repository = _IngressRepository()
    ids = Ids()
    now = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    handler = ReplayIngressHandler(
        factory,  # type: ignore[arg-type]
        cipher=RawPayloadCipher(b"k" * 32, key_id="raw-test-key"),
        id_generator=ids,
        now_factory=lambda: now,
        repository_factory=lambda _connection, _scope: repository,  # type: ignore[arg-type]
    )
    source = _input(
        state=BedPresenceState.IN_BED,
        at=datetime(2026, 8, 7, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        identity="bed-in",
    )
    result = handler.ingest(_scope(), ReplayRawBatch(items=(source,)))

    assert len(result) == 1
    assert uow.commits == 1
    assert len(factory.scopes) == 1
    assert repository.prepared[0].encrypted_payload != source.canonical_bytes()
    assert repository.prepared[0].payload_sha256


class _FastRepository:
    def __init__(
        self,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        *,
        current_revision_id: str | None = None,
    ) -> None:
        self.episode = episode
        self.current_revision_id = (
            current_revision_id or episode.current_night_episode_revision_id
        )
        self.observations = {item.observation_id: item for item in observations}
        self.quality = None
        self.risk = None
        self.persisted: dict[str, Any] | None = None

    def load_fast_path_operation(self, lease: FastPathLease) -> LoadedFastPathOperation:
        return LoadedFastPathOperation(
            operation_id=lease.operation_id,
            subject_id=self.episode.subject_id,
            night_episode_id=self.episode.night_episode_id,
            operation_json={
                "schema_version": "backend_operation.v2",
                "night_episode_revision_id": "revision-1",
            },
        )

    def lock_fast_path_episode_revision(
        self,
        operation: LoadedFastPathOperation,
    ) -> None:
        if (
            operation.operation_json["night_episode_revision_id"]
            != self.current_revision_id
        ):
            raise SleepSliceStaleRevision("stale test revision")

    def get_night_episode(self, _namespace: Any, *, night_episode_id: str) -> NightEpisode:
        assert night_episode_id == self.episode.night_episode_id
        return self.episode

    def get_quality_assessment(self, _namespace: Any, *, assessment_id: str) -> Any:
        del assessment_id
        return self.quality

    def get_current_risk(self, _namespace: Any, *, night_episode_id: str) -> Any:
        del night_episode_id
        return self.risk

    def get_observation(self, _namespace: Any, *, observation_id: str) -> SleepObservation:
        return self.observations[observation_id]

    def get_current_night_revision(self, _namespace: Any, *, night_episode_id: str) -> CurrentRevisionPointer:
        return CurrentRevisionPointer(night_episode_id, "revision-1", 1, 1)

    def list_vendor_alert_instances(self, *_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return ()

    def get_alert_correlation_receipt(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_fast_path_signal_projection(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def acquire_subject_lifecycle_lease(
        self,
        _namespace: Any,
        *,
        subject_id: str,
        lease_owner: str,
        lease_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> SubjectLifecycleLease:
        return SubjectLifecycleLease(
            subject_id,
            lease_owner,
            lease_token,
            now + lease_duration,
        )

    def release_subject_lifecycle_lease(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def next_domain_event_sequence(self, *_args: Any, **_kwargs: Any) -> int:
        return 1

    def commit_deterministic_fast_path(
        self,
        _namespace: Any,
        *,
        quality: Any,
        risk: Any,
        **_kwargs: Any,
    ) -> bool:
        self.quality = quality
        self.risk = risk
        return True

    def persist_fast_path_handoff(self, **kwargs: Any) -> None:
        self.persisted = kwargs


def _fast_observation(
    *,
    observation_id: str,
    payload: Any,
    at: datetime,
) -> SleepObservation:
    observation_type = payload.observation_type
    return SleepObservation(
        observation_id=observation_id,
        data_mode=DataMode.REPLAY,
        observation_type=observation_type,
        payload=payload,
        subject_id="subject-1",
        device_id="device-1",
        device_binding_id="binding-1",
        binding_version=1,
        event_occurred_at=at,
        received_at=at,
        timezone_status=TimezoneStatus.KNOWN,
        source_kind=(
            SourceKind.VENDOR_DERIVED
            if observation_type == ObservationType.VENDOR_ALERT
            else SourceKind.DEVICE_MEASURED
        ),
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="replay-provider",
            provider_account_id="replay-account",
            adapter_id="replay-observation-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=f"raw:{observation_id}",
            raw_payload_sha256="b" * 64,
        ),
        source_key=f"source:{observation_id}",
        idempotency_key=f"idem:{observation_id}",
    )


def test_urgent_fast_path_commits_zero_model_and_no_product_operation() -> None:
    start = datetime(2026, 8, 7, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    observations = (
        _fast_observation(
            observation_id="bed",
            payload=BedPresencePayload(state=BedPresenceState.IN_BED),
            at=end - timedelta(minutes=1),
        ),
        _fast_observation(
            observation_id="alert",
            payload=VendorAlertPayload(
                alert_code="possible_fall",
                lifecycle_state=AlertLifecycleState.ACTIVE,
                vendor_alert_instance_id="vendor-alert-1",
            ),
            at=end - timedelta(seconds=30),
        ),
    )
    episode = NightEpisode(
        night_episode_id="episode-1",
        data_mode=DataMode.REPLAY,
        subject_id="subject-1",
        timezone_name="UTC",
        local_sleep_date=date(2026, 8, 7),
        night_key="night-1",
        collection_start_at=start,
        collection_end_at=end,
        allowed_lateness_watermark_at=end + timedelta(hours=1),
        report_deadline_at=end + timedelta(hours=1),
        collection_window_derivation=CollectionWindowDerivation.VERIFIED_IN_BED,
        binding_references=(DeviceBindingReference(
            device_binding_id="binding-1",
            binding_version=1,
            device_id="device-1",
        ),),
        observation_ids=tuple(item.observation_id for item in observations),
        night_episode_revision_ids=("revision-1",),
        data_sufficiency=DataSufficiency.REPORT_PENDING,
        pinned_adapter_versions={"replay-observation-adapter": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={
            "quality": "quality-v1",
            "risk": "risk-v1",
            "fast_path_event": "fast-path-event-v1",
        },
        state=NightEpisodeState.AWAITING_REPORT,
        current_night_episode_revision_id="revision-1",
        created_at=start,
        updated_at=end,
    )
    repository = _FastRepository(episode, observations)
    uow = _FakeUow()
    handler = FastPathHandler(
        _FakeUowFactory(uow),  # type: ignore[arg-type]
        policy=_policy(urgent=True),
        id_generator=Ids(),
        now_factory=lambda: end + timedelta(seconds=1),
        repository_factory=lambda _connection, _scope: repository,  # type: ignore[arg-type]
    )
    result = handler.process(
        _scope(),
        FastPathLease(
            operation_id=uuid7_from_parts(
                int(end.timestamp() * 1000),
                99,
            ),
            lease_generation=1,
            fencing_token="f" * 32,
            worker_instance="worker-1",
        ),
    )

    assert result.urgent is True
    assert result.model_invocation_count == 0
    assert result.product_agent_operation_id is None
    assert repository.risk.risk_state == RiskState.REVIEWED_SIGNAL
    assert repository.persisted is not None
    assert repository.persisted["decision"].model_invocation_count == 0
    assert repository.persisted["product_agent_operation_id"] is None
    assert uow.commits == 1


def test_stale_fast_path_revision_cannot_overwrite_current_projections() -> None:
    start = datetime(2026, 8, 7, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    observation = _fast_observation(
        observation_id="bed",
        payload=BedPresencePayload(state=BedPresenceState.IN_BED),
        at=end - timedelta(minutes=1),
    )
    episode = NightEpisode(
        night_episode_id="episode-1",
        data_mode=DataMode.REPLAY,
        subject_id="subject-1",
        timezone_name="UTC",
        local_sleep_date=date(2026, 8, 7),
        night_key="night-1",
        collection_start_at=start,
        collection_end_at=end,
        allowed_lateness_watermark_at=end + timedelta(hours=1),
        report_deadline_at=end + timedelta(hours=1),
        collection_window_derivation=CollectionWindowDerivation.VERIFIED_IN_BED,
        binding_references=(
            DeviceBindingReference(
                device_binding_id="binding-1",
                binding_version=1,
                device_id="device-1",
            ),
        ),
        observation_ids=(observation.observation_id,),
        night_episode_revision_ids=("revision-2",),
        data_sufficiency=DataSufficiency.REPORT_PENDING,
        pinned_adapter_versions={"replay-observation-adapter": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={
            "quality": "quality-v1",
            "risk": "risk-v1",
            "fast_path_event": "fast-path-event-v1",
        },
        state=NightEpisodeState.AWAITING_REPORT,
        current_night_episode_revision_id="revision-2",
        created_at=start,
        updated_at=end,
    )
    repository = _FastRepository(
        episode,
        (observation,),
        current_revision_id="revision-2",
    )
    uow = _FakeUow()
    handler = FastPathHandler(
        _FakeUowFactory(uow),  # type: ignore[arg-type]
        policy=_policy(),
        id_generator=Ids(),
        now_factory=lambda: end + timedelta(seconds=1),
        repository_factory=lambda _connection, _scope: repository,  # type: ignore[arg-type]
    )

    with pytest.raises(SleepSliceStaleRevision):
        handler.process(
            _scope(),
            FastPathLease(
                operation_id=uuid7_from_parts(
                    int(end.timestamp() * 1000),
                    98,
                ),
                lease_generation=1,
                fencing_token="f" * 32,
                worker_instance="worker-1",
            ),
        )

    assert repository.quality is None
    assert repository.risk is None
    assert repository.persisted is None
    assert uow.commits == 0


class _RowsCursor:
    def __init__(self, rows: tuple[Any, ...]) -> None:
        self.rows = rows
        self.query = ""

    def execute(self, query: str, _params: Any = None) -> None:
        self.query = query

    def fetchall(self) -> tuple[Any, ...]:
        return self.rows

    def fetchone(self) -> Any | None:
        return None if not self.rows else self.rows[0]

    def close(self) -> None:
        return None


class _RowsConnection:
    def __init__(self, cursor: _RowsCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RowsCursor:
        return self._cursor


def _api_context() -> PostgresAuthenticatedActorContext:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    claims = ActorAssertionClaims(
        iss="sleepagent-bff-v1",
        aud="sleepagent-backend",
        jti="assertion-1",
        actor_id="actor-1",
        subject_id="subject-1",
        role=PublicActorRole.ELDER,
        scope=("sleep:episode:read",),
        iat=int(now.timestamp()),
        exp=int((now + timedelta(minutes=2)).timestamp()),
        nonce="n" * 16,
        method="GET",
        path="/api/v1/subjects/subject-1/night-episodes",
        body_sha256=hashlib.sha256(b"").hexdigest(),
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
    )
    authority = ResolvedActorAuthority(
        namespace_id="replay:pytest",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        binding_id="binding-authority-1",
        role=ProductRole.ELDER,
        effective_scopes=frozenset(claims.scope),
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
    )
    return PostgresAuthenticatedActorContext(
        service_principal=ServicePrincipal(
            principal_id="sleepagent-api-test",
            credential_id="credential-1",
        ),
        claims=claims,
        binding=AuthoritativeRoleBinding(
            authorization_id=authority.binding_id,
            actor_id=claims.actor_id,
            subject_id=claims.subject_id,
            role=claims.role,
            scopes=authority.effective_scopes,
            authorization_epoch=1,
        ),
        correlation_id="correlation-1",
        authority=authority,
    )


def test_public_episode_query_only_projects_finalized_nonconflict_v2_rows() -> None:
    committed = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    row = (
        "episode-1",
        "subject-1",
        date(2026, 8, 8),
        "Asia/Shanghai",
        committed - timedelta(hours=8),
        committed - timedelta(minutes=5),
        committed + timedelta(hours=2),
        "awaiting_report",
        2,
        "revision-2",
        committed,
        date(2026, 8, 7),
        date(2026, 8, 8),
        "observed_wake",
        "observed",
        False,
        date(2026, 8, 7),
        {
            "data_sufficiency": "sufficient",
            "reason_codes": ["quality_sufficient"],
        },
        committed - timedelta(hours=8),
    )
    cursor = _RowsCursor((row,))
    uow = _FakeUow()
    uow.connection = _RowsConnection(cursor)
    runtime = PostgresSleepApiRuntime(
        uow_factory=_FakeUowFactory(uow),  # type: ignore[arg-type]
        authenticator=object(),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )
    response = runtime.list_night_episodes(
        _api_context(),
        subject_id="subject-1",
        limit=20,
        cursor=None,
    )

    assert response.items[0].local_sleep_date == date(2026, 8, 7)
    assert response.items[0].episode_local_date == date(2026, 8, 8)
    assert response.items[0].assignment_basis == "observed_wake"
    assert "episode.date_state = 'finalized'" in cursor.query
    assert "episode.date_conflict = FALSE" in cursor.query
    assert "source_scope,night_episode_revision_id" in cursor.query
    assert "episode.current_revision_id" in cursor.query
    assert uow.commits == 1


def test_public_current_risk_rejects_a_stale_revision_projection() -> None:
    cursor = _RowsCursor(())
    uow = _FakeUow()
    uow.connection = _RowsConnection(cursor)
    runtime = PostgresSleepApiRuntime(
        uow_factory=_FakeUowFactory(uow),  # type: ignore[arg-type]
        authenticator=object(),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    with pytest.raises(SleepApiApplicationError) as raised:
        runtime.get_current_risk(_api_context(), subject_id="subject-1")

    assert raised.value.code == PublicErrorCode.DATA_INSUFFICIENT
    assert "source_scope,night_episode_revision_id" in cursor.query
    assert "episode.current_revision_id" in cursor.query


def test_default_role_view_is_pinned_to_the_current_episode_revision() -> None:
    cursor = _RowsCursor(())
    uow = _FakeUow()
    uow.connection = _RowsConnection(cursor)
    runtime = PostgresSleepApiRuntime(
        uow_factory=_FakeUowFactory(uow),  # type: ignore[arg-type]
        authenticator=object(),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    with pytest.raises(SleepApiApplicationError) as raised:
        runtime.get_role_view(
            _api_context(),
            subject_id="subject-1",
            night_episode_id="episode-1",
            revision_id=None,
        )

    assert raised.value.code == PublicErrorCode.RESULT_PENDING
    assert "view.night_episode_revision_id =" in cursor.query
    assert "episode.current_revision_id" in cursor.query


def test_public_command_adapter_reserves_a_real_sleep_command_operation() -> None:
    class CommandBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, dict[str, Any]]] = []

        def reserve_command(self, context: Any, **values: Any) -> str:
            self.calls.append((context, values))
            return "01987654-3210-7abc-8def-0123456789ab"

    backend = CommandBackend()
    runtime = PostgresSleepApiRuntime(
        uow_factory=object(),  # type: ignore[arg-type]
        authenticator=object(),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
        command_backend=backend,  # type: ignore[arg-type]
    )

    accepted = runtime.submit_command(
        _api_context(),
        operation_type="sleep_api.monitoring.activate.v1",
        route_template="/api/v1/subjects/{subject_id}/monitoring/activate",
        target_resource_id="subject-1",
        idempotency_key="activate-one",
        request_payload={
            "schema_version": "activate_monitoring_request.v1",
            "device_binding_id": None,
            "occurred_at": None,
        },
    )

    assert accepted.status == "pending"
    assert accepted.operation_id == "01987654-3210-7abc-8def-0123456789ab"
    assert accepted.status_url.endswith(accepted.operation_id)
    context, values = backend.calls[0]
    assert context.purpose == "sleep_care"
    assert values["command_type"] == "sleep_api.monitoring.activate.v1"
    assert len(values["body_sha256"]) == 64


class _StaticPostgresAuthenticator:
    def __init__(self, context: PostgresAuthenticatedActorContext) -> None:
        self.context = context

    def authorize_identity(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> PostgresAuthenticatedActorContext:
        return self.context


def test_expired_postgres_event_cursor_requires_snapshot_resync() -> None:
    now = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    context = _api_context()
    cursor = _RowsCursor(())
    uow = _FakeUow()
    uow.connection = _RowsConnection(cursor)
    runtime = PostgresSleepApiRuntime(
        uow_factory=_FakeUowFactory(uow),  # type: ignore[arg-type]
        authenticator=_StaticPostgresAuthenticator(context),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
        now_factory=lambda: now,
    )
    expired = runtime.cursor_codec.encode(
        {
            "kind": "sleep_event_cursor.pg.v1",
            "authority": runtime._cursor_authority(context),
            "delivery_offset": 3,
            "expires_at": (now - timedelta(seconds=1)).isoformat(),
            "event_schema_generation": "sleep-domain-events-v2",
        }
    )

    with pytest.raises(SleepApiApplicationError) as raised:
        runtime.poll_events(
            object(),  # type: ignore[arg-type]
            subject_id="subject-1",
            cursor=expired,
            limit=20,
        )

    assert raised.value.code == PublicErrorCode.CURSOR_RESYNC_REQUIRED
    assert raised.value.status_code == 409
    assert cursor.query == ""
