"""Elder-centered NightEpisode aggregation over canonical observations.

The service is deterministic and persistence-backed. It does not run an Agent,
generate a role report, or apply physiological/medical thresholds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleepagent.sleep_domain.contracts import (
    AssociationKind,
    AssociationStatus,
    BedExitPayload,
    BedPresencePayload,
    BedPresenceState,
    CollectionWindowDerivation,
    DataMode,
    DataSufficiency,
    DeviceBinding,
    DeviceBindingReference,
    DomainEvent,
    DomainEventType,
    EpisodeBoundaryPolicy,
    LifecycleComponent,
    LifecycleTransitionPolicy,
    LifecycleTransitionReceipt,
    LifecycleTrigger,
    LifecycleTriggerKind,
    LifecycleTriggerSource,
    MonitoringSnapshot,
    MonitoringState,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeState,
    NightRevisionCause,
    ObservationType,
    PendingEpisodeAssociation,
    SleepObservation,
)
from sleepagent.sleep_domain.lifecycle import (
    LifecycleAccessPolicy,
    LifecycleBusyError,
    InvalidLifecycleTransitionError,
    decide_monitoring_transition,
    require_night_episode_transition,
)
from sleepagent.sleep_domain.repository import (
    CasConflictError,
    DomainNamespace,
    EpisodeObservationMembership,
    NightRevisionCommitResult,
    SleepDomainRepository,
    SourceReportVersion,
)


class EpisodeAggregationStatus(str, Enum):
    OPENED = "opened"
    TRANSITIONED = "transitioned"
    ASSOCIATED = "associated"
    REVISED = "revised"
    PUBLISHED = "published"
    PENDING_ASSOCIATION = "pending_association"
    SUPPRESSED = "suppressed"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True)
class EpisodeVersionPins:
    adapter_versions: Mapping[str, str]
    observation_schema_versions: tuple[str, ...]
    policy_versions: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapter_versions",
            MappingProxyType(dict(self.adapter_versions)),
        )
        object.__setattr__(
            self,
            "policy_versions",
            MappingProxyType(dict(self.policy_versions)),
        )
        if not self.observation_schema_versions:
            raise ValueError("at least one observation schema version must be pinned")


@dataclass(frozen=True)
class EpisodeAggregationResult:
    status: EpisodeAggregationStatus
    episode: NightEpisode | None
    monitoring: MonitoringSnapshot | None
    revision: NightEpisodeRevision | None = None
    pending_association: PendingEpisodeAssociation | None = None
    reason_code: str | None = None


class NightEpisodeService:
    def __init__(
        self,
        repository: SleepDomainRepository,
        *,
        boundary_policies: Mapping[str, EpisodeBoundaryPolicy],
        active_boundary_policy_version: str,
        transition_policy: LifecycleTransitionPolicy,
        transition_policies: Mapping[
            str, LifecycleTransitionPolicy
        ] | None = None,
        access_policy: LifecycleAccessPolicy,
        default_version_pins: EpisodeVersionPins,
        worker_id: str = "night-episode-worker",
    ) -> None:
        self.repository = repository
        self.boundary_policies = MappingProxyType(dict(boundary_policies))
        if active_boundary_policy_version not in self.boundary_policies:
            raise ValueError("active boundary policy must be retained")
        self.active_boundary_policy_version = active_boundary_policy_version
        retained_transition_policies = dict(transition_policies or {})
        retained_transition_policies[transition_policy.policy_version] = (
            transition_policy
        )
        self.transition_policies = MappingProxyType(
            retained_transition_policies
        )
        self.transition_policy = transition_policy
        self.access_policy = access_policy
        self.default_version_pins = default_version_pins
        self.worker_id = worker_id

    @property
    def active_boundary_policy(self) -> EpisodeBoundaryPolicy:
        return self.boundary_policies[self.active_boundary_policy_version]

    def process_observation(
        self,
        namespace: DomainNamespace,
        *,
        observation_id: str,
        processed_at: datetime,
    ) -> EpisodeAggregationResult:
        observation = self.repository.get_observation(
            namespace,
            observation_id=observation_id,
        )
        if observation is None:
            raise KeyError(f"canonical observation not found: {observation_id}")
        event_at, _ = self._observation_event_time(
            observation,
            self.active_boundary_policy,
        )
        if event_at is None:
            return self._record_pending_observation(
                namespace,
                observation,
                event_at=None,
                candidates=(),
                reason_code="event_time_unavailable",
                created_at=processed_at,
            )

        kind: LifecycleTriggerKind | None = None
        if isinstance(observation.payload, BedPresencePayload):
            if observation.payload.state == BedPresenceState.IN_BED:
                kind = LifecycleTriggerKind.IN_BED
            elif observation.payload.state == BedPresenceState.OUT_OF_BED:
                kind = LifecycleTriggerKind.OUT_OF_BED
        elif isinstance(observation.payload, BedExitPayload):
            kind = LifecycleTriggerKind.OUT_OF_BED

        if kind is None:
            return self.associate_observation(
                namespace,
                observation_id=observation_id,
                associated_at=processed_at,
            )
        trigger = LifecycleTrigger(
            trigger_id=f"observation-trigger:{observation.observation_id}",
            data_mode=observation.data_mode,
            subject_id=observation.subject_id,
            source=LifecycleTriggerSource.VERIFIED_DEVICE_EVENT,
            kind=kind,
            occurred_at=event_at,
            received_at=max(processed_at, event_at),
            observation_id=observation.observation_id,
            correlation_id=f"observation:{observation.observation_id}",
        )
        return self.process_trigger(
            namespace,
            trigger=trigger,
            observation=observation,
        )

    def process_trigger(
        self,
        namespace: DomainNamespace,
        *,
        trigger: LifecycleTrigger,
        observation: SleepObservation | None = None,
        device_binding_id: str | None = None,
        version_pins: EpisodeVersionPins | None = None,
    ) -> EpisodeAggregationResult:
        if trigger.data_mode != namespace.data_mode:
            raise ValueError("trigger data_mode does not match namespace")
        self.access_policy.authorize(trigger)
        lease_token = f"{self.worker_id}:{uuid4()}"
        lease = self.repository.acquire_subject_lifecycle_lease(
            namespace,
            subject_id=trigger.subject_id,
            lease_owner=self.worker_id,
            lease_token=lease_token,
            now=trigger.received_at,
            lease_duration=timedelta(
                seconds=self.transition_policy.lease_seconds
            ),
        )
        if lease is None:
            raise LifecycleBusyError(
                f"subject lifecycle is leased: {trigger.subject_id}"
            )
        try:
            if trigger.kind in {
                LifecycleTriggerKind.IN_BED,
                LifecycleTriggerKind.ACTIVATE,
                LifecycleTriggerKind.FALLBACK_START,
            }:
                return self._start_or_resume(
                    namespace,
                    trigger,
                    observation=observation,
                    device_binding_id=device_binding_id,
                    version_pins=version_pins,
                )
            if trigger.kind in {
                LifecycleTriggerKind.OUT_OF_BED,
                LifecycleTriggerKind.DEACTIVATE,
                LifecycleTriggerKind.FALLBACK_END,
            }:
                return self._end_collection(
                    namespace,
                    trigger,
                    observation=observation,
                )
            raise InvalidLifecycleTransitionError(
                f"trigger {trigger.kind.value} is not a monitoring transition"
            )
        finally:
            self.repository.release_subject_lifecycle_lease(namespace, lease)

    def associate_observation(
        self,
        namespace: DomainNamespace,
        *,
        observation_id: str,
        associated_at: datetime,
    ) -> EpisodeAggregationResult:
        existing = self.repository.get_episode_observation_membership(
            namespace,
            observation_id=observation_id,
        )
        if existing is not None:
            episode = self.repository.get_night_episode(
                namespace,
                night_episode_id=existing.night_episode_id,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=episode,
                monitoring=(
                    None
                    if episode is None
                    else self.repository.get_monitoring_snapshot(
                        namespace,
                        subject_id=episode.subject_id,
                    )
                ),
                reason_code="observation_already_associated",
            )
        observation = self.repository.get_observation(
            namespace,
            observation_id=observation_id,
        )
        if observation is None:
            raise KeyError(f"canonical observation not found: {observation_id}")
        binding = self._validated_observation_binding(namespace, observation)
        candidates = self._matching_episodes_for_observation(
            namespace,
            observation,
            binding,
        )
        event_at, fallback = self._observation_event_time(
            observation,
            self.active_boundary_policy,
        )
        if len(candidates) != 1 or event_at is None:
            reason = (
                "event_time_unavailable"
                if event_at is None
                else "no_unique_night_episode"
            )
            return self._record_pending_observation(
                namespace,
                observation,
                event_at=event_at,
                candidates=candidates,
                reason_code=reason,
                created_at=associated_at,
            )
        episode = candidates[0]
        adapter_pin = episode.pinned_adapter_versions.get(
            observation.provenance.adapter_id
        )
        if (
            adapter_pin is not None
            and adapter_pin != observation.provenance.adapter_version
        ):
            return self._record_pending_observation(
                namespace,
                observation,
                event_at=event_at,
                candidates=(episode,),
                reason_code="pinned_adapter_version_mismatch",
                created_at=associated_at,
            )
        if (
            observation.schema_version
            not in episode.pinned_observation_schema_versions
        ):
            return self._record_pending_observation(
                namespace,
                observation,
                event_at=event_at,
                candidates=(episode,),
                reason_code="pinned_observation_schema_version_mismatch",
                created_at=associated_at,
            )
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        late_after_watermark = bool(
            episode.allowed_lateness_watermark_at is not None
            and observation.received_at > episode.allowed_lateness_watermark_at
        )
        membership = self._membership(
            episode,
            observation,
            event_at=event_at,
            associated_at=associated_at,
            late_after_watermark=late_after_watermark,
        )
        quality_flags = set(episode.quality_flags)
        if fallback:
            quality_flags.add("receipt_time_fallback")
        if late_after_watermark:
            quality_flags.add("late_after_watermark")
        observation_ids = tuple(sorted({*episode.observation_ids, observation_id}))
        updated = episode.model_copy(
            update={
                "observation_ids": observation_ids,
                "quality_flags": tuple(sorted(quality_flags)),
                "updated_at": associated_at,
            }
        )
        pending = self.repository.get_pending_episode_association(
            namespace,
            association_kind=AssociationKind.OBSERVATION.value,
            source_resource_id=observation_id,
        )
        resolved_pending = self._resolved_pending(
            pending,
            episode_id=episode.night_episode_id,
            resolved_at=associated_at,
        )
        if pointer.current_revision_id is None:
            self.repository.commit_episode_observation_membership(
                namespace,
                episode=updated,
                expected_episode_cas=pointer.cas_version,
                membership=membership,
                pending_association=resolved_pending,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.ASSOCIATED,
                episode=updated,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=episode.subject_id,
                ),
                reason_code=(
                    "late_after_watermark"
                    if late_after_watermark
                    else "event_time_membership"
                ),
            )
        if episode.state not in {
            NightEpisodeState.ANALYZED,
            NightEpisodeState.CLOSED,
            NightEpisodeState.REVISED,
        }:
            raise InvalidLifecycleTransitionError(
                "an episode with a current revision cannot accept an "
                f"observation while {episode.state.value}"
            )
        return self._publish_revision(
            namespace,
            episode=updated,
            trigger_id=f"late-observation:{observation_id}",
            cause=NightRevisionCause.LATE_OBSERVATION,
            created_at=associated_at,
            membership=membership,
            pending_association=resolved_pending,
        )

    def publish_report_deadline(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        trigger_id: str,
        published_at: datetime,
    ) -> EpisodeAggregationResult:
        episode = self._require_episode(namespace, night_episode_id)
        if episode.report_deadline_at is None:
            raise ValueError("NightEpisode has no report deadline")
        if published_at < episode.report_deadline_at:
            raise ValueError("report deadline has not been reached")
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=night_episode_id,
        )
        if pointer.current_revision_id is not None:
            revision = self.repository.get_night_episode_revision(
                namespace,
                night_episode_revision_id=pointer.current_revision_id,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=episode,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=episode.subject_id,
                ),
                revision=revision,
                reason_code="deadline_revision_already_published",
            )
        if episode.state != NightEpisodeState.AWAITING_REPORT:
            raise InvalidLifecycleTransitionError(
                "report deadline requires awaiting_report state"
            )
        reports = self.repository.list_episode_source_reports(
            namespace,
            night_episode_id=night_episode_id,
        )
        if reports:
            sufficiency = (
                DataSufficiency.DATA_INSUFFICIENT
                if all(report.is_empty for report in reports)
                else DataSufficiency.SUFFICIENT
            )
            cause = NightRevisionCause.INITIAL_PUBLICATION
        else:
            sufficiency = (
                DataSufficiency.DATA_INSUFFICIENT
                if not episode.observation_ids
                else DataSufficiency.REPORT_PENDING
            )
            cause = NightRevisionCause.REPORT_DEADLINE
        flags = set(episode.quality_flags)
        if not reports:
            flags.add("vendor_report_pending")
        if sufficiency == DataSufficiency.DATA_INSUFFICIENT:
            flags.add("data_insufficient")
        updated = episode.model_copy(
            update={
                "data_sufficiency": sufficiency,
                "quality_flags": tuple(sorted(flags)),
                "updated_at": published_at,
            }
        )
        return self._publish_revision(
            namespace,
            episode=updated,
            trigger_id=trigger_id,
            cause=cause,
            created_at=published_at,
        )

    def associate_source_report(
        self,
        namespace: DomainNamespace,
        *,
        source_report_version_id: str,
        associated_at: datetime,
    ) -> EpisodeAggregationResult:
        report = self.repository.get_source_report_version(
            namespace,
            source_report_version_id=source_report_version_id,
        )
        if report is None:
            raise KeyError(
                f"source report version not found: {source_report_version_id}"
            )
        bindings = self.repository.matching_device_bindings(
            namespace,
            provider_id=report.provider_id,
            provider_account_id=report.provider_account_id,
            provider_device_key=report.provider_device_key,
        )
        candidates: list[NightEpisode] = []
        subjects = {binding.subject_id for binding in bindings}
        for subject_id in subjects:
            for episode in self.repository.list_night_episodes(
                namespace,
                subject_id=subject_id,
            ):
                binding_ids = {
                    reference.device_binding_id
                    for reference in episode.binding_references
                }
                if (
                    episode.local_sleep_date == report.local_report_date
                    and any(
                        binding.device_binding_id in binding_ids
                        for binding in bindings
                    )
                ):
                    candidates.append(episode)
        if len(candidates) != 1:
            association = self._pending_association(
                namespace,
                kind=AssociationKind.SOURCE_REPORT,
                source_resource_id=source_report_version_id,
                subject_id=next(iter(subjects)) if len(subjects) == 1 else None,
                event_at=None,
                binding_id=None,
                candidates=tuple(candidates),
                reason_code="no_unique_report_episode",
                created_at=associated_at,
            )
            self.repository.append_pending_episode_association(
                namespace,
                association,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.PENDING_ASSOCIATION,
                episode=None,
                monitoring=None,
                pending_association=association,
                reason_code=association.reason_code,
            )
        episode = candidates[0]
        if source_report_version_id in episode.source_report_references:
            pointer = self.repository.get_current_night_revision(
                namespace,
                night_episode_id=episode.night_episode_id,
            )
            revision = (
                None
                if pointer.current_revision_id is None
                else self.repository.get_night_episode_revision(
                    namespace,
                    night_episode_revision_id=pointer.current_revision_id,
                )
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=episode,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=episode.subject_id,
                ),
                revision=revision,
                reason_code="source_report_already_linked",
            )
        if episode.state == NightEpisodeState.COLLECTING:
            association = self._pending_association(
                namespace,
                kind=AssociationKind.SOURCE_REPORT,
                source_resource_id=source_report_version_id,
                subject_id=episode.subject_id,
                event_at=None,
                binding_id=None,
                candidates=(episode,),
                reason_code="episode_still_collecting",
                created_at=associated_at,
            )
            self.repository.append_pending_episode_association(
                namespace,
                association,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.PENDING_ASSOCIATION,
                episode=episode,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=episode.subject_id,
                ),
                pending_association=association,
                reason_code=association.reason_code,
            )
        quality_flags = set(episode.quality_flags)
        quality_flags.discard("vendor_report_pending")
        if report.is_empty:
            quality_flags.add("data_insufficient")
        else:
            quality_flags.discard("data_insufficient")
        updated = episode.model_copy(
            update={
                "source_report_references": tuple(
                    sorted(
                        {
                            *episode.source_report_references,
                            source_report_version_id,
                        }
                    )
                ),
                "data_sufficiency": (
                    DataSufficiency.DATA_INSUFFICIENT
                    if report.is_empty
                    else DataSufficiency.SUFFICIENT
                ),
                "quality_flags": tuple(sorted(quality_flags)),
                "updated_at": associated_at,
            }
        )
        pending = self.repository.get_pending_episode_association(
            namespace,
            association_kind=AssociationKind.SOURCE_REPORT.value,
            source_resource_id=source_report_version_id,
        )
        resolved_pending = self._resolved_pending(
            pending,
            episode_id=episode.night_episode_id,
            resolved_at=associated_at,
        )
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        cause = (
            NightRevisionCause.INITIAL_PUBLICATION
            if pointer.current_revision_id is None
            else NightRevisionCause.CORRECTED_VENDOR_REPORT
        )
        return self._publish_revision(
            namespace,
            episode=updated,
            trigger_id=f"source-report:{source_report_version_id}",
            cause=cause,
            created_at=associated_at,
            source_report=report,
            pending_association=resolved_pending,
        )

    def request_reanalysis(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        trigger: LifecycleTrigger,
    ) -> EpisodeAggregationResult:
        if trigger.kind != LifecycleTriggerKind.REANALYZE:
            raise ValueError("explicit reanalysis requires reanalyze trigger")
        if trigger.source not in {
            LifecycleTriggerSource.AUTHORIZED_COMMAND,
            LifecycleTriggerSource.EXPLICIT_REANALYSIS,
        }:
            raise ValueError("explicit reanalysis requires an authorized source")
        self.access_policy.authorize(trigger)
        episode = self._require_episode(namespace, night_episode_id)
        if episode.subject_id != trigger.subject_id:
            raise ValueError("reanalysis subject does not match NightEpisode")
        if episode.current_night_episode_revision_id is None:
            raise InvalidLifecycleTransitionError(
                "explicit reanalysis requires an existing immutable revision"
            )
        return self._publish_revision(
            namespace,
            episode=episode.model_copy(update={"updated_at": trigger.received_at}),
            trigger_id=trigger.trigger_id,
            cause=NightRevisionCause.EXPLICIT_REANALYSIS,
            created_at=trigger.received_at,
            audit_trigger=trigger,
        )

    def submit_feedback_revision(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        trigger: LifecycleTrigger,
    ) -> EpisodeAggregationResult:
        """Append the immutable NightEpisode revision caused by feedback.

        Feedback content and provenance are stored by the public API boundary;
        this lifecycle method records only its committed domain effect.
        """

        if trigger.kind != LifecycleTriggerKind.FEEDBACK:
            raise ValueError("feedback revision requires a feedback trigger")
        if trigger.source != LifecycleTriggerSource.AUTHORIZED_COMMAND:
            raise ValueError("feedback revision requires an authorized source")
        self.access_policy.authorize(trigger)
        episode = self._require_episode(namespace, night_episode_id)
        if episode.subject_id != trigger.subject_id:
            raise ValueError("feedback subject does not match NightEpisode")
        if episode.current_night_episode_revision_id is None:
            raise InvalidLifecycleTransitionError(
                "feedback requires an existing immutable revision"
            )
        return self._publish_revision(
            namespace,
            episode=episode.model_copy(update={"updated_at": trigger.received_at}),
            trigger_id=trigger.trigger_id,
            cause=NightRevisionCause.FEEDBACK,
            created_at=trigger.received_at,
            audit_trigger=trigger,
        )

    def close_episode(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        trigger: LifecycleTrigger,
    ) -> EpisodeAggregationResult:
        self.access_policy.authorize(trigger)
        episode = self._require_episode(namespace, night_episode_id)
        require_night_episode_transition(
            episode.state,
            NightEpisodeState.CLOSED,
        )
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=night_episode_id,
        )
        updated = episode.model_copy(
            update={
                "state": NightEpisodeState.CLOSED,
                "updated_at": trigger.received_at,
                "transition_receipt_ids": (
                    *episode.transition_receipt_ids,
                    self._transition_receipt_id(
                        LifecycleComponent.NIGHT_EPISODE,
                        episode.night_episode_id,
                        trigger.trigger_id,
                    ),
                ),
            }
        )
        receipt = self._transition_receipt(
            component=LifecycleComponent.NIGHT_EPISODE,
            aggregate_id=episode.night_episode_id,
            trigger=trigger,
            from_state=episode.state.value,
            to_state=NightEpisodeState.CLOSED.value,
            reason_code="current_revision_closed",
            policy_version=self._transition_for_episode(
                episode
            ).policy_version,
        )
        event = self._event(
            namespace,
            event_type=DomainEventType.NIGHT_EPISODE_CLOSED,
            aggregate_type="NightEpisode",
            aggregate_id=episode.night_episode_id,
            aggregate_version=pointer.cas_version + 1,
            subject_id=episode.subject_id,
            trigger=trigger,
            night_episode_id=episode.night_episode_id,
            revision_id=pointer.current_revision_id,
            attributes={"state": NightEpisodeState.CLOSED.value},
        )
        self.repository.commit_lifecycle_transition(
            namespace,
            monitoring_snapshot=None,
            expected_monitoring_cas=None,
            episode=updated,
            expected_episode_cas=pointer.cas_version,
            receipts=(receipt,),
            events=(event,),
        )
        return EpisodeAggregationResult(
            status=EpisodeAggregationStatus.TRANSITIONED,
            episode=updated,
            monitoring=self.repository.get_monitoring_snapshot(
                namespace,
                subject_id=episode.subject_id,
            ),
            reason_code="episode_closed",
        )

    def recover_subject(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
    ) -> EpisodeAggregationResult:
        snapshot = self.repository.get_monitoring_snapshot(
            namespace,
            subject_id=subject_id,
        )
        if snapshot is None:
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=None,
                monitoring=None,
                reason_code="no_persisted_lifecycle",
            )
        episode = (
            None
            if snapshot.active_night_episode_id is None
            else self.repository.get_night_episode(
                namespace,
                night_episode_id=snapshot.active_night_episode_id,
            )
        )
        if snapshot.state == MonitoringState.ACTIVE and (
            episode is None or episode.state != NightEpisodeState.COLLECTING
        ):
            raise InvalidLifecycleTransitionError(
                "persisted active monitoring does not reference a collecting "
                "NightEpisode"
            )
        return EpisodeAggregationResult(
            status=EpisodeAggregationStatus.IDEMPOTENT,
            episode=episode,
            monitoring=snapshot,
            reason_code="recovered_from_persistent_state",
        )

    def _start_or_resume(
        self,
        namespace: DomainNamespace,
        trigger: LifecycleTrigger,
        *,
        observation: SleepObservation | None,
        device_binding_id: str | None,
        version_pins: EpisodeVersionPins | None,
    ) -> EpisodeAggregationResult:
        binding = self._binding_for_trigger(
            namespace,
            trigger,
            observation=observation,
            device_binding_id=device_binding_id,
        )
        self._require_fallback_schedule(trigger, binding.timezone_name)
        boundary = self.active_boundary_policy
        night_key = boundary.derive_night_key(
            subject_id=trigger.subject_id,
            event_at=trigger.occurred_at,
            timezone_name=binding.timezone_name,
        )
        existing = [
            episode
            for episode in self.repository.list_night_episodes(
                namespace,
                subject_id=trigger.subject_id,
            )
            if episode.night_key == night_key
        ]
        if len(existing) > 1:
            raise InvalidLifecycleTransitionError(
                "multiple NightEpisodes share one deterministic night_key"
            )
        episode = existing[0] if existing else None
        episode_id = (
            episode.night_episode_id
            if episode is not None
            else self._episode_id(namespace, trigger.subject_id, night_key)
        )
        snapshot = self._monitoring_snapshot(
            namespace,
            trigger,
        )
        decision = decide_monitoring_transition(
            snapshot,
            trigger,
            self.transition_policy,
            active_night_episode_id=episode_id,
        )
        if decision.suppressed:
            if observation is not None:
                return self._record_pending_observation(
                    namespace,
                    observation,
                    event_at=trigger.occurred_at,
                    candidates=(() if episode is None else (episode,)),
                    reason_code=decision.reason_code,
                    created_at=trigger.received_at,
                )
            return EpisodeAggregationResult(
                status=(
                    EpisodeAggregationStatus.IDEMPOTENT
                    if decision.reason_code == "duplicate_trigger"
                    else EpisodeAggregationStatus.SUPPRESSED
                ),
                episode=episode,
                monitoring=snapshot,
                reason_code=decision.reason_code,
            )
        if episode is not None and episode.state != NightEpisodeState.COLLECTING:
            if observation is not None:
                return self.associate_observation(
                    namespace,
                    observation_id=observation.observation_id,
                    associated_at=trigger.received_at,
                )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.SUPPRESSED,
                episode=episode,
                monitoring=snapshot,
                reason_code="night_key_already_finalized",
            )

        pins = self._pins_for_open(observation, version_pins)
        membership: EpisodeObservationMembership | None = None
        if episode is None:
            local_date = boundary.derive_local_sleep_date(
                trigger.occurred_at,
                binding.timezone_name,
            )
            report_deadline, dst_flag = self._report_deadline(
                local_date,
                binding.timezone_name,
                boundary,
            )
            if report_deadline <= trigger.occurred_at:
                report_deadline, later_dst_flag = self._report_deadline(
                    local_date + timedelta(days=1),
                    binding.timezone_name,
                    boundary,
                )
                dst_flag = later_dst_flag or "report_deadline_rolled_forward"
            quality_flags = () if dst_flag is None else (dst_flag,)
            observation_ids = (
                () if observation is None else (observation.observation_id,)
            )
            episode = NightEpisode(
                night_episode_id=episode_id,
                data_mode=namespace.data_mode,
                subject_id=trigger.subject_id,
                timezone_name=binding.timezone_name,
                local_sleep_date=local_date,
                night_key=night_key,
                collection_start_at=trigger.occurred_at,
                collection_window_derivation=self._derivation(trigger),
                binding_references=(self._binding_reference(binding),),
                observation_ids=observation_ids,
                data_sufficiency=DataSufficiency.PARTIAL,
                quality_flags=quality_flags,
                pinned_adapter_versions=dict(pins.adapter_versions),
                pinned_observation_schema_versions=(
                    pins.observation_schema_versions
                ),
                pinned_policy_versions={
                    **dict(pins.policy_versions),
                    "night_boundary": boundary.policy_version,
                    "lifecycle_transition": (
                        self.transition_policy.policy_version
                    ),
                },
                state=NightEpisodeState.COLLECTING,
                report_deadline_at=report_deadline,
                transition_receipt_ids=(
                    self._transition_receipt_id(
                        LifecycleComponent.NIGHT_EPISODE,
                        episode_id,
                        trigger.trigger_id,
                    ),
                ),
                created_at=trigger.received_at,
                updated_at=trigger.received_at,
            )
            if observation is not None:
                membership = self._membership(
                    episode,
                    observation,
                    event_at=trigger.occurred_at,
                    associated_at=trigger.received_at,
                    late_after_watermark=False,
                )
            expected_episode_cas = 0
            episode_receipt = self._transition_receipt(
                component=LifecycleComponent.NIGHT_EPISODE,
                aggregate_id=episode_id,
                trigger=trigger,
                from_state="none",
                to_state=NightEpisodeState.COLLECTING.value,
                reason_code="elder_centered_night_opened",
            )
            episode_event = self._event(
                namespace,
                event_type=DomainEventType.ELDER_IN_BED,
                aggregate_type="NightEpisode",
                aggregate_id=episode_id,
                aggregate_version=1,
                subject_id=trigger.subject_id,
                trigger=trigger,
                night_episode_id=episode_id,
                attributes={
                    "night_key": night_key,
                    "timezone_name": binding.timezone_name,
                    "derivation": episode.collection_window_derivation.value,
                },
            )
        else:
            pointer = self.repository.get_current_night_revision(
                namespace,
                night_episode_id=episode_id,
            )
            expected_episode_cas = pointer.cas_version
            episode_receipt = None
            episode_event = None
            if observation is not None and (
                observation.observation_id not in episode.observation_ids
            ):
                episode = episode.model_copy(
                    update={
                        "observation_ids": tuple(
                            sorted(
                                {
                                    *episode.observation_ids,
                                    observation.observation_id,
                                }
                            )
                        ),
                        "updated_at": trigger.received_at,
                    }
                )
                membership = self._membership(
                    episode,
                    observation,
                    event_at=trigger.occurred_at,
                    associated_at=trigger.received_at,
                    late_after_watermark=False,
                )
            else:
                expected_episode_cas = None
                episode = None

        receipts: list[LifecycleTransitionReceipt] = []
        events: list[DomainEvent] = []
        if decision.state_changed:
            receipts.append(
                self._transition_receipt(
                    component=LifecycleComponent.MONITORING,
                    aggregate_id=trigger.subject_id,
                    trigger=trigger,
                    from_state=snapshot.state.value,
                    to_state=decision.snapshot.state.value,
                    reason_code=decision.reason_code,
                )
            )
            events.append(
                self._event(
                    namespace,
                    event_type=DomainEventType.MONITORING_ACTIVATED,
                    aggregate_type="Monitoring",
                    aggregate_id=trigger.subject_id,
                    aggregate_version=decision.snapshot.cas_version,
                    subject_id=trigger.subject_id,
                    trigger=trigger,
                    night_episode_id=episode_id,
                    attributes={"state": MonitoringState.ACTIVE.value},
                )
            )
        if episode_receipt is not None and episode_event is not None:
            receipts.append(episode_receipt)
            events.append(episode_event)
        self.repository.commit_lifecycle_transition(
            namespace,
            monitoring_snapshot=(
                decision.snapshot if decision.snapshot_changed else None
            ),
            expected_monitoring_cas=(
                snapshot.cas_version if decision.snapshot_changed else None
            ),
            episode=episode,
            expected_episode_cas=expected_episode_cas,
            receipts=tuple(receipts),
            events=tuple(events),
            memberships=(() if membership is None else (membership,)),
        )
        stored_episode = self._require_episode(namespace, episode_id)
        return EpisodeAggregationResult(
            status=(
                EpisodeAggregationStatus.OPENED
                if episode_receipt is not None
                else EpisodeAggregationStatus.TRANSITIONED
            ),
            episode=stored_episode,
            monitoring=(
                decision.snapshot
                if decision.snapshot_changed
                else snapshot
            ),
            reason_code=decision.reason_code,
        )

    def _end_collection(
        self,
        namespace: DomainNamespace,
        trigger: LifecycleTrigger,
        *,
        observation: SleepObservation | None,
    ) -> EpisodeAggregationResult:
        snapshot = self._monitoring_snapshot(namespace, trigger)
        episode = (
            None
            if snapshot.active_night_episode_id is None
            else self.repository.get_night_episode(
                namespace,
                night_episode_id=snapshot.active_night_episode_id,
            )
        )
        episode_policy = (
            self.transition_policy
            if episode is None
            else self._transition_for_episode(episode)
        )
        if episode is not None:
            self._require_fallback_schedule(
                trigger,
                episode.timezone_name,
                episode_policy,
            )
        is_manual = trigger.source == LifecycleTriggerSource.AUTHORIZED_COMMAND
        if (
            episode is not None
            and episode.state == NightEpisodeState.COLLECTING
            and not is_manual
            and trigger.occurred_at
            < episode.collection_start_at
            + timedelta(
                seconds=episode_policy.minimum_collecting_dwell_seconds
            )
        ):
            if observation is not None:
                return self.associate_observation(
                    namespace,
                    observation_id=observation.observation_id,
                    associated_at=trigger.received_at,
                )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.SUPPRESSED,
                episode=episode,
                monitoring=snapshot,
                reason_code="minimum_collecting_dwell_not_met",
            )
        decision = decide_monitoring_transition(
            snapshot,
            trigger,
            episode_policy,
            active_night_episode_id=None,
        )
        if decision.suppressed:
            if observation is not None:
                return self.associate_observation(
                    namespace,
                    observation_id=observation.observation_id,
                    associated_at=trigger.received_at,
                )
            return EpisodeAggregationResult(
                status=(
                    EpisodeAggregationStatus.IDEMPOTENT
                    if decision.reason_code == "duplicate_trigger"
                    else EpisodeAggregationStatus.SUPPRESSED
                ),
                episode=episode,
                monitoring=snapshot,
                reason_code=decision.reason_code,
            )
        if episode is None or episode.state != NightEpisodeState.COLLECTING:
            self.repository.commit_lifecycle_transition(
                namespace,
                monitoring_snapshot=(
                    decision.snapshot if decision.snapshot_changed else None
                ),
                expected_monitoring_cas=(
                    snapshot.cas_version if decision.snapshot_changed else None
                ),
                episode=None,
                expected_episode_cas=None,
                receipts=(),
                events=(),
            )
            if observation is not None:
                return self.associate_observation(
                    namespace,
                    observation_id=observation.observation_id,
                    associated_at=trigger.received_at,
                )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=episode,
                monitoring=decision.snapshot,
                reason_code="no_collecting_episode",
            )

        require_night_episode_transition(
            episode.state,
            NightEpisodeState.AWAITING_REPORT,
        )
        boundary = self._boundary_for_episode(episode)
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        observation_ids = set(episode.observation_ids)
        membership: EpisodeObservationMembership | None = None
        if observation is not None and observation.observation_id not in observation_ids:
            self._validated_observation_binding(namespace, observation)
            observation_ids.add(observation.observation_id)
            membership = self._membership(
                episode,
                observation,
                event_at=trigger.occurred_at,
                associated_at=trigger.received_at,
                late_after_watermark=False,
            )
        watermark = trigger.occurred_at + timedelta(
            seconds=boundary.allowed_lateness_seconds
        )
        transition_id = self._transition_receipt_id(
            LifecycleComponent.NIGHT_EPISODE,
            episode.night_episode_id,
            trigger.trigger_id,
        )
        updated_episode = episode.model_copy(
            update={
                "collection_end_at": trigger.occurred_at,
                "allowed_lateness_watermark_at": watermark,
                "observation_ids": tuple(sorted(observation_ids)),
                "state": NightEpisodeState.AWAITING_REPORT,
                "transition_receipt_ids": (
                    *episode.transition_receipt_ids,
                    transition_id,
                ),
                "updated_at": trigger.received_at,
            }
        )
        episode_receipt = self._transition_receipt(
            component=LifecycleComponent.NIGHT_EPISODE,
            aggregate_id=episode.night_episode_id,
            trigger=trigger,
            from_state=NightEpisodeState.COLLECTING.value,
            to_state=NightEpisodeState.AWAITING_REPORT.value,
            reason_code="collection_end_trigger",
            policy_version=episode_policy.policy_version,
        )
        receipts = [episode_receipt]
        events = [
            self._event(
                namespace,
                event_type=DomainEventType.NIGHT_EPISODE_AWAITING_REPORT,
                aggregate_type="NightEpisode",
                aggregate_id=episode.night_episode_id,
                aggregate_version=pointer.cas_version + 1,
                subject_id=episode.subject_id,
                trigger=trigger,
                night_episode_id=episode.night_episode_id,
                attributes={
                    "lateness_watermark_at": watermark.isoformat(),
                    "report_deadline_at": (
                        None
                        if episode.report_deadline_at is None
                        else episode.report_deadline_at.isoformat()
                    ),
                },
            )
        ]
        if decision.state_changed:
            receipts.insert(
                0,
                self._transition_receipt(
                    component=LifecycleComponent.MONITORING,
                    aggregate_id=trigger.subject_id,
                    trigger=trigger,
                    from_state=snapshot.state.value,
                    to_state=MonitoringState.DORMANT.value,
                    reason_code=decision.reason_code,
                    policy_version=episode_policy.policy_version,
                ),
            )
            events.insert(
                0,
                self._event(
                    namespace,
                    event_type=DomainEventType.MONITORING_DORMANT,
                    aggregate_type="Monitoring",
                    aggregate_id=trigger.subject_id,
                    aggregate_version=decision.snapshot.cas_version,
                    subject_id=trigger.subject_id,
                    trigger=trigger,
                    night_episode_id=episode.night_episode_id,
                    attributes={"state": MonitoringState.DORMANT.value},
                ),
            )
        self.repository.commit_lifecycle_transition(
            namespace,
            monitoring_snapshot=(
                decision.snapshot if decision.snapshot_changed else None
            ),
            expected_monitoring_cas=(
                snapshot.cas_version if decision.snapshot_changed else None
            ),
            episode=updated_episode,
            expected_episode_cas=pointer.cas_version,
            receipts=tuple(receipts),
            events=tuple(events),
            memberships=(() if membership is None else (membership,)),
        )
        return EpisodeAggregationResult(
            status=EpisodeAggregationStatus.TRANSITIONED,
            episode=updated_episode,
            monitoring=decision.snapshot,
            reason_code="awaiting_vendor_report",
        )

    def _publish_revision(
        self,
        namespace: DomainNamespace,
        *,
        episode: NightEpisode,
        trigger_id: str,
        cause: NightRevisionCause,
        created_at: datetime,
        membership: EpisodeObservationMembership | None = None,
        source_report: SourceReportVersion | None = None,
        pending_association: PendingEpisodeAssociation | None = None,
        audit_trigger: LifecycleTrigger | None = None,
    ) -> EpisodeAggregationResult:
        publication_id = self._stable_id(
            "revision-publication",
            namespace.namespace_id,
            episode.night_episode_id,
            trigger_id,
        )
        revision_id = self._stable_id(
            "night-revision",
            namespace.namespace_id,
            episode.night_episode_id,
            trigger_id,
        )
        existing = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=revision_id,
        )
        if existing is not None:
            stored_episode = self._require_episode(
                namespace,
                episode.night_episode_id,
            )
            return EpisodeAggregationResult(
                status=EpisodeAggregationStatus.IDEMPOTENT,
                episode=stored_episode,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=episode.subject_id,
                ),
                revision=existing,
                reason_code="revision_trigger_already_committed",
            )

        for _ in range(3):
            current = self._require_episode(
                namespace,
                episode.night_episode_id,
            )
            pointer = self.repository.get_current_night_revision(
                namespace,
                night_episode_id=current.night_episode_id,
            )
            merged_observations = tuple(
                sorted({*current.observation_ids, *episode.observation_ids})
            )
            merged_reports = tuple(
                sorted(
                    {
                        *current.source_report_references,
                        *episode.source_report_references,
                    }
                )
            )
            reports = self.repository.list_episode_source_reports(
                namespace,
                night_episode_id=current.night_episode_id,
            )
            report_by_id = {
                report.source_report_version_id: report for report in reports
            }
            if source_report is not None:
                report_by_id[source_report.source_report_version_id] = source_report
            report_hashes = tuple(
                report_by_id[report_id].content_sha256
                for report_id in merged_reports
                if report_id in report_by_id
            )
            if len(report_hashes) != len(merged_reports):
                raise ValueError(
                    "every linked source-report version must remain addressable"
                )
            revision_number = (pointer.current_revision_number or 0) + 1
            parent_id = pointer.current_revision_id
            next_state = (
                NightEpisodeState.ANALYZED
                if parent_id is None
                else NightEpisodeState.REVISED
            )
            transition_receipt: LifecycleTransitionReceipt | None = None
            transition_ids = current.transition_receipt_ids
            if current.state != next_state:
                require_night_episode_transition(current.state, next_state)
                receipt_trigger = audit_trigger or LifecycleTrigger(
                    trigger_id=trigger_id,
                    data_mode=namespace.data_mode,
                    subject_id=current.subject_id,
                    source=(
                        LifecycleTriggerSource.REPORT_DEADLINE
                        if cause == NightRevisionCause.REPORT_DEADLINE
                        else (
                            LifecycleTriggerSource.SOURCE_REPORT
                            if source_report is not None
                            else LifecycleTriggerSource.VERIFIED_DEVICE_EVENT
                        )
                    ),
                    kind=(
                        LifecycleTriggerKind.REPORT_DEADLINE_REACHED
                        if cause == NightRevisionCause.REPORT_DEADLINE
                        else (
                            LifecycleTriggerKind.SOURCE_REPORT_AVAILABLE
                            if source_report is not None
                            else LifecycleTriggerKind.IN_BED
                        )
                    ),
                    occurred_at=created_at,
                    received_at=created_at,
                    observation_id=(
                        None if membership is None else membership.observation_id
                    ),
                    source_report_version_id=(
                        None
                        if source_report is None
                        else source_report.source_report_version_id
                    ),
                    correlation_id=f"revision:{revision_id}",
                )
                transition_receipt = self._transition_receipt(
                    component=LifecycleComponent.NIGHT_EPISODE,
                    aggregate_id=current.night_episode_id,
                    trigger=receipt_trigger,
                    from_state=current.state.value,
                    to_state=next_state.value,
                    reason_code=f"revision:{cause.value}",
                    policy_version=self._transition_for_episode(
                        current
                    ).policy_version,
                )
                transition_ids = (
                    *transition_ids,
                    transition_receipt.transition_receipt_id,
                )
            updated = current.model_copy(
                update={
                    "observation_ids": merged_observations,
                    "source_report_references": merged_reports,
                    "night_episode_revision_ids": (
                        *current.night_episode_revision_ids,
                        revision_id,
                    ),
                    "transition_receipt_ids": transition_ids,
                    "data_sufficiency": episode.data_sufficiency,
                    "quality_flags": tuple(sorted(episode.quality_flags)),
                    "state": next_state,
                    "current_night_episode_revision_id": revision_id,
                    "updated_at": created_at,
                }
            )
            revision = NightEpisodeRevision(
                night_episode_revision_id=revision_id,
                night_episode_id=current.night_episode_id,
                data_mode=namespace.data_mode,
                subject_id=current.subject_id,
                revision_number=revision_number,
                parent_revision_id=parent_id,
                revision_cause=cause,
                observation_ids=merged_observations,
                source_report_references=merged_reports,
                source_report_sha256=report_hashes,
                observation_set_sha256=self._observation_set_hash(
                    namespace,
                    merged_observations,
                ),
                data_sufficiency=episode.data_sufficiency,
                quality_flags=updated.quality_flags,
                created_at=created_at,
            )
            event_type = (
                DomainEventType.NIGHT_EPISODE_ANALYZED
                if revision_number == 1
                else DomainEventType.NIGHT_EPISODE_REVISED
            )
            event = self._event(
                namespace,
                event_type=event_type,
                aggregate_type="NightEpisode",
                aggregate_id=current.night_episode_id,
                aggregate_version=revision_number,
                subject_id=current.subject_id,
                trigger=audit_trigger
                or LifecycleTrigger(
                    trigger_id=trigger_id,
                    data_mode=namespace.data_mode,
                    subject_id=current.subject_id,
                    source=LifecycleTriggerSource.RESTART_RECOVERY,
                    kind=LifecycleTriggerKind.RECOVER,
                    occurred_at=created_at,
                    received_at=created_at,
                    correlation_id=f"revision:{revision_id}",
                ),
                night_episode_id=current.night_episode_id,
                revision_id=revision_id,
                attributes={
                    "revision_number": revision_number,
                    "revision_cause": cause.value,
                    "data_sufficiency": episode.data_sufficiency.value,
                    "supersedes_revision": parent_id,
                },
            )
            try:
                committed = self.repository.commit_night_revision(
                    namespace,
                    publication_id=publication_id,
                    trigger_id=trigger_id,
                    revision=revision,
                    episode=updated,
                    expected_episode_cas=pointer.cas_version,
                    expected_current_revision_id=pointer.current_revision_id,
                    event=event,
                    transition_receipt=transition_receipt,
                    membership=membership,
                    source_report=source_report,
                    pending_association=pending_association,
                )
            except CasConflictError:
                existing = self.repository.get_night_episode_revision(
                    namespace,
                    night_episode_revision_id=revision_id,
                )
                if existing is not None:
                    return EpisodeAggregationResult(
                        status=EpisodeAggregationStatus.IDEMPOTENT,
                        episode=self._require_episode(
                            namespace,
                            current.night_episode_id,
                        ),
                        monitoring=self.repository.get_monitoring_snapshot(
                            namespace,
                            subject_id=current.subject_id,
                        ),
                        revision=existing,
                        reason_code="concurrent_revision_winner",
                    )
                continue
            return EpisodeAggregationResult(
                status=(
                    EpisodeAggregationStatus.PUBLISHED
                    if revision.revision_number == 1
                    else EpisodeAggregationStatus.REVISED
                ),
                episode=committed.episode,
                monitoring=self.repository.get_monitoring_snapshot(
                    namespace,
                    subject_id=current.subject_id,
                ),
                revision=committed.revision,
                reason_code=cause.value,
            )
        raise CasConflictError("NightEpisode revision publication did not converge")

    def _matching_episodes_for_observation(
        self,
        namespace: DomainNamespace,
        observation: SleepObservation,
        binding: DeviceBinding,
    ) -> tuple[NightEpisode, ...]:
        matches: list[NightEpisode] = []
        for episode in self.repository.list_night_episodes(
            namespace,
            subject_id=observation.subject_id,
        ):
            boundary = self._boundary_for_episode(episode)
            event_at, _ = self._observation_event_time(observation, boundary)
            if event_at is None:
                continue
            if episode.timezone_name != binding.timezone_name:
                continue
            if not any(
                reference.device_binding_id == observation.device_binding_id
                and reference.binding_version == observation.binding_version
                for reference in episode.binding_references
            ):
                continue
            if boundary.derive_night_key(
                subject_id=observation.subject_id,
                event_at=event_at,
                timezone_name=episode.timezone_name,
            ) != episode.night_key:
                continue
            if (
                event_at
                > episode.collection_start_at
                + timedelta(seconds=boundary.maximum_episode_seconds)
            ):
                continue
            if (
                episode.collection_end_at is not None
                and event_at > episode.collection_end_at
            ):
                continue
            matches.append(episode)
        return tuple(matches)

    def _validated_observation_binding(
        self,
        namespace: DomainNamespace,
        observation: SleepObservation,
    ) -> DeviceBinding:
        binding = self.repository.get_device_binding(
            namespace,
            device_binding_id=observation.device_binding_id,
        )
        if binding is None:
            raise ValueError("canonical observation binding no longer exists")
        event_at, _ = self._observation_event_time(
            observation,
            self.active_boundary_policy,
        )
        if event_at is None:
            raise ValueError("canonical observation has no trustworthy event time")
        if (
            binding.subject_id != observation.subject_id
            or binding.device_id != observation.device_id
            or binding.binding_version != observation.binding_version
            or event_at < binding.effective_from
            or (
                binding.effective_until is not None
                and event_at >= binding.effective_until
            )
        ):
            raise ValueError(
                "canonical observation no longer validates against its exact "
                "binding interval"
            )
        return binding

    def _binding_for_trigger(
        self,
        namespace: DomainNamespace,
        trigger: LifecycleTrigger,
        *,
        observation: SleepObservation | None,
        device_binding_id: str | None,
    ) -> DeviceBinding:
        if observation is not None:
            if observation.observation_id != trigger.observation_id:
                raise ValueError("trigger observation id does not match observation")
            return self._validated_observation_binding(namespace, observation)
        if not device_binding_id:
            raise ValueError(
                "command/time fallback opening requires an exact binding id"
            )
        binding = self.repository.get_device_binding(
            namespace,
            device_binding_id=device_binding_id,
        )
        if binding is None or binding.subject_id != trigger.subject_id:
            raise ValueError("trigger binding does not match subject")
        if (
            trigger.occurred_at < binding.effective_from
            or (
                binding.effective_until is not None
                and trigger.occurred_at >= binding.effective_until
            )
        ):
            raise ValueError("trigger time falls outside binding interval")
        return binding

    def _pins_for_open(
        self,
        observation: SleepObservation | None,
        supplied: EpisodeVersionPins | None,
    ) -> EpisodeVersionPins:
        if supplied is not None:
            return supplied
        if observation is None:
            return self.default_version_pins
        return EpisodeVersionPins(
            adapter_versions={
                observation.provenance.adapter_id: (
                    observation.provenance.adapter_version
                )
            },
            observation_schema_versions=(observation.schema_version,),
            policy_versions=dict(self.default_version_pins.policy_versions),
        )

    def _monitoring_snapshot(
        self,
        namespace: DomainNamespace,
        trigger: LifecycleTrigger,
    ) -> MonitoringSnapshot:
        stored = self.repository.get_monitoring_snapshot(
            namespace,
            subject_id=trigger.subject_id,
        )
        if stored is not None:
            return stored
        return MonitoringSnapshot(
            data_mode=namespace.data_mode,
            subject_id=trigger.subject_id,
            state=MonitoringState.DORMANT,
            state_entered_at=trigger.occurred_at
            - timedelta(
                seconds=self.transition_policy.minimum_dormant_dwell_seconds
            ),
            cas_version=0,
            updated_at=trigger.occurred_at,
        )

    def _boundary_for_episode(
        self,
        episode: NightEpisode,
    ) -> EpisodeBoundaryPolicy:
        version = episode.pinned_policy_versions.get("night_boundary")
        if not version or version not in self.boundary_policies:
            raise ValueError(
                "NightEpisode pins an unavailable boundary policy version"
            )
        return self.boundary_policies[version]

    def _transition_for_episode(
        self,
        episode: NightEpisode,
    ) -> LifecycleTransitionPolicy:
        version = episode.pinned_policy_versions.get("lifecycle_transition")
        if not version or version not in self.transition_policies:
            raise ValueError(
                "NightEpisode pins an unavailable transition policy version"
            )
        return self.transition_policies[version]

    def _require_fallback_schedule(
        self,
        trigger: LifecycleTrigger,
        timezone_name: str,
        policy: LifecycleTransitionPolicy | None = None,
    ) -> None:
        if trigger.source not in {
            LifecycleTriggerSource.TIME_FALLBACK,
            LifecycleTriggerSource.RESTART_RECOVERY,
        }:
            return
        resolved_policy = policy or self.transition_policy
        if trigger.kind == LifecycleTriggerKind.FALLBACK_START:
            target = resolved_policy.fallback_start_local_minute
        elif trigger.kind == LifecycleTriggerKind.FALLBACK_END:
            target = resolved_policy.fallback_end_local_minute
        else:
            return
        local = trigger.occurred_at.astimezone(ZoneInfo(timezone_name))
        actual = local.hour * 60 + local.minute
        minute_delta = abs(actual - target)
        circular_delta = min(minute_delta, 1440 - minute_delta)
        if circular_delta * 60 > resolved_policy.fallback_tolerance_seconds:
            raise InvalidLifecycleTransitionError(
                "time fallback trigger falls outside the pinned schedule window"
            )

    @staticmethod
    def _observation_event_time(
        observation: SleepObservation,
        policy: EpisodeBoundaryPolicy,
    ) -> tuple[datetime | None, bool]:
        if observation.event_occurred_at is not None:
            return observation.event_occurred_at, False
        if observation.measurement_at is not None:
            return observation.measurement_at, False
        if policy.allow_received_at_fallback:
            return observation.received_at, True
        return None, False

    def _record_pending_observation(
        self,
        namespace: DomainNamespace,
        observation: SleepObservation,
        *,
        event_at: datetime | None,
        candidates: tuple[NightEpisode, ...],
        reason_code: str,
        created_at: datetime,
    ) -> EpisodeAggregationResult:
        association = self._pending_association(
            namespace,
            kind=AssociationKind.OBSERVATION,
            source_resource_id=observation.observation_id,
            subject_id=observation.subject_id,
            event_at=event_at,
            binding_id=observation.device_binding_id,
            candidates=candidates,
            reason_code=reason_code,
            created_at=created_at,
        )
        stored = self.repository.append_pending_episode_association(
            namespace,
            association,
        )
        return EpisodeAggregationResult(
            status=EpisodeAggregationStatus.PENDING_ASSOCIATION,
            episode=None if len(candidates) != 1 else candidates[0],
            monitoring=self.repository.get_monitoring_snapshot(
                namespace,
                subject_id=observation.subject_id,
            ),
            pending_association=stored,
            reason_code=stored.reason_code,
        )

    def _pending_association(
        self,
        namespace: DomainNamespace,
        *,
        kind: AssociationKind,
        source_resource_id: str,
        subject_id: str | None,
        event_at: datetime | None,
        binding_id: str | None,
        candidates: tuple[NightEpisode, ...],
        reason_code: str,
        created_at: datetime,
    ) -> PendingEpisodeAssociation:
        return PendingEpisodeAssociation(
            association_id=self._stable_id(
                "pending-association",
                namespace.namespace_id,
                kind.value,
                source_resource_id,
            ),
            data_mode=namespace.data_mode,
            subject_id=subject_id,
            association_kind=kind,
            source_resource_id=source_resource_id,
            event_at=event_at,
            binding_id=binding_id,
            candidate_night_episode_ids=tuple(
                sorted(episode.night_episode_id for episode in candidates)
            ),
            status=AssociationStatus.PENDING,
            reason_code=reason_code,
            created_at=created_at,
        )

    @staticmethod
    def _resolved_pending(
        pending: PendingEpisodeAssociation | None,
        *,
        episode_id: str,
        resolved_at: datetime,
    ) -> PendingEpisodeAssociation | None:
        if pending is None or pending.status != AssociationStatus.PENDING:
            return None
        return pending.model_copy(
            update={
                "status": AssociationStatus.ASSOCIATED,
                "reason_code": "uniquely_associated",
                "resolved_at": resolved_at,
                "resolved_night_episode_id": episode_id,
            }
        )

    @staticmethod
    def _membership(
        episode: NightEpisode,
        observation: SleepObservation,
        *,
        event_at: datetime,
        associated_at: datetime,
        late_after_watermark: bool,
    ) -> EpisodeObservationMembership:
        membership_id = NightEpisodeService._stable_id(
            "episode-membership",
            episode.night_episode_id,
            observation.observation_id,
        )
        return EpisodeObservationMembership(
            membership_id=membership_id,
            night_episode_id=episode.night_episode_id,
            observation_id=observation.observation_id,
            subject_id=observation.subject_id,
            device_binding_id=observation.device_binding_id,
            binding_version=observation.binding_version,
            event_at=event_at,
            received_at=observation.received_at,
            lateness_watermark_at=episode.allowed_lateness_watermark_at,
            late_after_watermark=late_after_watermark,
            associated_at=associated_at,
        )

    def _observation_set_hash(
        self,
        namespace: DomainNamespace,
        observation_ids: tuple[str, ...],
    ) -> str:
        digest = hashlib.sha256()
        for observation_id in sorted(observation_ids):
            observation = self.repository.get_observation(
                namespace,
                observation_id=observation_id,
            )
            if observation is None:
                raise ValueError(
                    f"revision observation is missing: {observation_id}"
                )
            digest.update(observation_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(
                hashlib.sha256(
                    observation.model_dump_json().encode("utf-8")
                ).digest()
            )
        return digest.hexdigest()

    def _event(
        self,
        namespace: DomainNamespace,
        *,
        event_type: DomainEventType,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        subject_id: str,
        trigger: LifecycleTrigger,
        night_episode_id: str | None,
        revision_id: str | None = None,
        attributes: Mapping[str, str | int | float | bool | None],
    ) -> DomainEvent:
        return DomainEvent(
            event_id=self._stable_id(
                "domain-event",
                namespace.namespace_id,
                aggregate_type,
                aggregate_id,
                trigger.trigger_id,
                event_type.value,
            ),
            event_type=event_type,
            event_version="1",
            data_mode=namespace.data_mode,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=max(1, aggregate_version),
            per_aggregate_sequence=self.repository.next_domain_event_sequence(
                namespace,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
            ),
            delivery_offset=1,
            subject_id=subject_id,
            night_episode_id=night_episode_id,
            night_episode_revision_id=revision_id,
            event_occurred_at=trigger.occurred_at,
            persisted_at=trigger.received_at,
            correlation_id=trigger.correlation_id,
            causation_id=trigger.trigger_id,
            attributes=dict(attributes),
        )

    def _transition_receipt(
        self,
        *,
        component: LifecycleComponent,
        aggregate_id: str,
        trigger: LifecycleTrigger,
        from_state: str,
        to_state: str,
        reason_code: str,
        policy_version: str | None = None,
    ) -> LifecycleTransitionReceipt:
        return LifecycleTransitionReceipt(
            transition_receipt_id=self._transition_receipt_id(
                component,
                aggregate_id,
                trigger.trigger_id,
            ),
            data_mode=trigger.data_mode,
            component=component,
            aggregate_id=aggregate_id,
            subject_id=trigger.subject_id,
            trigger_id=trigger.trigger_id,
            trigger_source=trigger.source,
            trigger_kind=trigger.kind,
            policy_version=policy_version or self.transition_policy.policy_version,
            from_state=from_state,
            to_state=to_state,
            reason_code=reason_code,
            occurred_at=trigger.occurred_at,
            committed_at=trigger.received_at,
        )

    @staticmethod
    def _transition_receipt_id(
        component: LifecycleComponent,
        aggregate_id: str,
        trigger_id: str,
    ) -> str:
        return NightEpisodeService._stable_id(
            "lifecycle-transition",
            component.value,
            aggregate_id,
            trigger_id,
        )

    @staticmethod
    def _binding_reference(binding: DeviceBinding) -> DeviceBindingReference:
        return DeviceBindingReference(
            device_binding_id=binding.device_binding_id,
            binding_version=binding.binding_version,
            device_id=binding.device_id,
        )

    @staticmethod
    def _derivation(
        trigger: LifecycleTrigger,
    ) -> CollectionWindowDerivation:
        if trigger.source == LifecycleTriggerSource.VERIFIED_DEVICE_EVENT:
            return CollectionWindowDerivation.VERIFIED_IN_BED
        if trigger.source == LifecycleTriggerSource.AUTHORIZED_COMMAND:
            return CollectionWindowDerivation.EXTERNAL_COMMAND
        if trigger.source in {
            LifecycleTriggerSource.TIME_FALLBACK,
            LifecycleTriggerSource.RESTART_RECOVERY,
        }:
            return CollectionWindowDerivation.TIME_FALLBACK
        return CollectionWindowDerivation.UNKNOWN

    @staticmethod
    def _episode_id(
        namespace: DomainNamespace,
        subject_id: str,
        night_key: str,
    ) -> str:
        return NightEpisodeService._stable_id(
            "night-episode",
            namespace.namespace_id,
            subject_id,
            night_key,
        )

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"

    def _require_episode(
        self,
        namespace: DomainNamespace,
        night_episode_id: str,
    ) -> NightEpisode:
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=night_episode_id,
        )
        if episode is None:
            raise KeyError(f"NightEpisode not found: {night_episode_id}")
        return episode

    @staticmethod
    def _report_deadline(
        local_sleep_date: date,
        timezone_name: str,
        policy: EpisodeBoundaryPolicy,
    ) -> tuple[datetime, str | None]:
        zone = ZoneInfo(timezone_name)
        deadline_date = local_sleep_date + timedelta(days=1)
        hour, minute = divmod(policy.report_deadline_local_minute, 60)
        naive = datetime.combine(deadline_date, time(hour=hour, minute=minute))
        valid: list[datetime] = []
        for fold in (0, 1):
            local = naive.replace(tzinfo=zone, fold=fold)
            roundtrip = local.astimezone(timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == naive and roundtrip.fold == fold:
                valid.append(local)
        unique = {
            candidate.astimezone(timezone.utc): candidate for candidate in valid
        }
        if len(unique) == 1:
            return next(iter(unique.values())), None
        if len(unique) == 2:
            later = max(unique.values(), key=lambda item: item.astimezone(timezone.utc))
            return later, "dst_ambiguous_deadline_late_fold"
        shifted = naive
        for _ in range(180):
            shifted += timedelta(minutes=1)
            local = shifted.replace(tzinfo=zone, fold=0)
            roundtrip = local.astimezone(timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == shifted:
                return local, "dst_nonexistent_deadline_shifted_forward"
        raise ValueError("could not resolve report deadline through DST transition")


__all__ = [
    "EpisodeAggregationResult",
    "EpisodeAggregationStatus",
    "EpisodeVersionPins",
    "NightEpisodeService",
]
