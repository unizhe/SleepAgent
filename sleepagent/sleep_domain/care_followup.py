"""Deterministic CareFollowup state machine and read-only ServiceMode view."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from sleepagent.sleep_domain.contracts import (
    CareFollowupCommand,
    CareFollowupSnapshot,
    CareFollowupState,
    CareFollowupTransitionReceipt,
    DomainEvent,
    DomainEventType,
    MonitoringState,
    ServiceMode,
    ServiceModeProjection,
)
from sleepagent.sleep_domain.lifecycle import LifecycleBusyError
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    SleepDomainRepository,
)


class CareFollowupError(RuntimeError):
    pass


class CareFollowupAuthorizationError(CareFollowupError):
    pass


class InvalidCareFollowupTransitionError(CareFollowupError):
    pass


@dataclass(frozen=True)
class CareFollowupAccessPolicy:
    """Deployment-owned grants for authorized care workflow commands."""

    grants: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "grants",
            MappingProxyType(
                {
                    str(actor_id): frozenset(
                        str(item) for item in authorization_ids
                    )
                    for actor_id, authorization_ids in self.grants.items()
                }
            ),
        )

    def authorize(self, command: CareFollowupCommand) -> None:
        allowed = self.grants.get(command.actor_id, frozenset())
        if command.authorization_id not in allowed:
            raise CareFollowupAuthorizationError(
                "care follow-up authorization was not granted by deployment policy"
            )


@dataclass(frozen=True)
class CareFollowupTransitionResult:
    snapshot: CareFollowupSnapshot
    receipt: CareFollowupTransitionReceipt
    event: DomainEvent | None
    created: bool


_ALLOWED_TRANSITIONS = frozenset(
    {
        (CareFollowupState.NONE, CareFollowupState.PENDING_FEEDBACK),
        (
            CareFollowupState.PENDING_FEEDBACK,
            CareFollowupState.FOLLOWING_UP,
        ),
        (CareFollowupState.PENDING_FEEDBACK, CareFollowupState.ENDED),
        (CareFollowupState.FOLLOWING_UP, CareFollowupState.COMPLETED),
        (CareFollowupState.FOLLOWING_UP, CareFollowupState.ENDED),
    }
)

_EVENT_TYPES = {
    CareFollowupState.PENDING_FEEDBACK: (
        DomainEventType.CARE_FOLLOWUP_PENDING
    ),
    CareFollowupState.FOLLOWING_UP: (
        DomainEventType.CARE_FOLLOWUP_STARTED
    ),
    CareFollowupState.COMPLETED: (
        DomainEventType.CARE_FOLLOWUP_COMPLETED
    ),
    CareFollowupState.ENDED: DomainEventType.CARE_FOLLOWUP_ENDED,
}


class CareFollowupService:
    def __init__(
        self,
        repository: SleepDomainRepository,
        *,
        access_policy: CareFollowupAccessPolicy,
        worker_id: str = "care-followup",
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("care follow-up lease_seconds must be positive")
        self.repository = repository
        self.access_policy = access_policy
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def transition(
        self,
        namespace: DomainNamespace,
        command: CareFollowupCommand,
    ) -> CareFollowupTransitionResult:
        if command.data_mode != namespace.data_mode:
            raise ValueError("care command data_mode does not match namespace")
        self.access_policy.authorize(command)
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=command.night_episode_id,
        )
        if episode is None:
            raise KeyError(
                f"NightEpisode not found: {command.night_episode_id}"
            )
        if episode.subject_id != command.subject_id:
            raise ValueError("care command subject does not own NightEpisode")

        duplicate = self.repository.get_care_followup_transition_receipt(
            namespace,
            night_episode_id=command.night_episode_id,
            command_id=command.command_id,
        )
        if duplicate is not None:
            self._require_duplicate_matches(command, duplicate)
            snapshot = self.repository.get_care_followup(
                namespace,
                night_episode_id=command.night_episode_id,
            )
            if snapshot is None:
                raise RuntimeError(
                    "care transition receipt exists without a snapshot"
                )
            return CareFollowupTransitionResult(
                snapshot=snapshot,
                receipt=duplicate,
                event=None,
                created=False,
            )

        lease = self.repository.acquire_subject_lifecycle_lease(
            namespace,
            subject_id=command.subject_id,
            lease_owner=self.worker_id,
            lease_token=f"{self.worker_id}:{uuid4()}",
            now=command.occurred_at,
            lease_duration=timedelta(seconds=self.lease_seconds),
        )
        if lease is None:
            raise LifecycleBusyError(
                f"subject care follow-up is leased: {command.subject_id}"
            )
        try:
            current = self.repository.get_care_followup(
                namespace,
                night_episode_id=command.night_episode_id,
            )
            from_state = (
                CareFollowupState.NONE
                if current is None
                else current.state
            )
            if (
                current is not None
                and command.occurred_at < current.state_entered_at
            ):
                raise InvalidCareFollowupTransitionError(
                    "care follow-up command precedes the current state"
                )
            if (from_state, command.target_state) not in _ALLOWED_TRANSITIONS:
                raise InvalidCareFollowupTransitionError(
                    "invalid CareFollowup transition: "
                    f"{from_state.value} -> {command.target_state.value}"
                )
            expected_cas = 0 if current is None else current.cas_version
            receipt = CareFollowupTransitionReceipt(
                receipt_id=self._stable_id(
                    "care-followup-receipt",
                    namespace.namespace_id,
                    command.night_episode_id,
                    command.command_id,
                ),
                command_id=command.command_id,
                data_mode=namespace.data_mode,
                subject_id=command.subject_id,
                night_episode_id=command.night_episode_id,
                from_state=from_state,
                to_state=command.target_state,
                actor_id=command.actor_id,
                authorization_id=command.authorization_id,
                reason_code=command.reason_code,
                occurred_at=command.occurred_at,
            )
            snapshot = CareFollowupSnapshot(
                data_mode=namespace.data_mode,
                subject_id=command.subject_id,
                night_episode_id=command.night_episode_id,
                state=command.target_state,
                state_entered_at=command.occurred_at,
                last_transition_id=receipt.receipt_id,
                cas_version=expected_cas + 1,
                updated_at=command.occurred_at,
            )
            event = self._event(
                namespace,
                command,
                receipt,
                aggregate_version=snapshot.cas_version,
            )
            created = self.repository.commit_care_followup_transition(
                namespace,
                snapshot=snapshot,
                expected_cas_version=expected_cas,
                receipt=receipt,
                event=event,
            )
            if not created:
                stored = self.repository.get_care_followup(
                    namespace,
                    night_episode_id=command.night_episode_id,
                )
                if stored is None:
                    raise RuntimeError(
                        "idempotent care transition lost its snapshot"
                    )
                return CareFollowupTransitionResult(
                    snapshot=stored,
                    receipt=receipt,
                    event=None,
                    created=False,
                )
            return CareFollowupTransitionResult(
                snapshot=snapshot,
                receipt=receipt,
                event=event,
                created=True,
            )
        finally:
            self.repository.release_subject_lifecycle_lease(namespace, lease)

    def _event(
        self,
        namespace: DomainNamespace,
        command: CareFollowupCommand,
        receipt: CareFollowupTransitionReceipt,
        *,
        aggregate_version: int,
    ) -> DomainEvent:
        return DomainEvent(
            event_id=self._stable_id(
                "domain-event",
                namespace.namespace_id,
                receipt.receipt_id,
            ),
            event_type=_EVENT_TYPES[command.target_state],
            event_version="1",
            data_mode=namespace.data_mode,
            aggregate_type="CareFollowup",
            aggregate_id=command.night_episode_id,
            aggregate_version=aggregate_version,
            per_aggregate_sequence=(
                self.repository.next_domain_event_sequence(
                    namespace,
                    aggregate_type="CareFollowup",
                    aggregate_id=command.night_episode_id,
                )
            ),
            delivery_offset=1,
            subject_id=command.subject_id,
            night_episode_id=command.night_episode_id,
            event_occurred_at=command.occurred_at,
            persisted_at=command.occurred_at,
            correlation_id=command.correlation_id,
            causation_id=command.command_id,
            attributes={
                "from_state": receipt.from_state.value,
                "to_state": receipt.to_state.value,
                "actor_id": command.actor_id,
                "authorization_id": command.authorization_id,
                "reason_code": command.reason_code,
            },
        )

    @staticmethod
    def _require_duplicate_matches(
        command: CareFollowupCommand,
        receipt: CareFollowupTransitionReceipt,
    ) -> None:
        if (
            receipt.data_mode != command.data_mode
            or receipt.subject_id != command.subject_id
            or receipt.night_episode_id != command.night_episode_id
            or receipt.to_state != command.target_state
            or receipt.actor_id != command.actor_id
            or receipt.authorization_id != command.authorization_id
            or receipt.reason_code != command.reason_code
            or receipt.occurred_at != command.occurred_at
        ):
            raise InvalidCareFollowupTransitionError(
                "care command id was already used with different content"
            )

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"


class ServiceModeProjector:
    """Compute the public label without persisting or writing component state."""

    def __init__(self, repository: SleepDomainRepository) -> None:
        self.repository = repository

    def project(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        projected_at: datetime,
    ) -> ServiceModeProjection:
        monitoring = self.repository.get_monitoring_snapshot(
            namespace,
            subject_id=subject_id,
        )
        monitoring_state = (
            MonitoringState.DORMANT
            if monitoring is None
            else monitoring.state
        )
        followups = self.repository.list_care_followups(
            namespace,
            subject_id=subject_id,
        )
        open_episode_ids = tuple(
            sorted(
                followup.night_episode_id
                for followup in followups
                if followup.state
                in {
                    CareFollowupState.PENDING_FEEDBACK,
                    CareFollowupState.FOLLOWING_UP,
                }
            )
        )
        if monitoring_state == MonitoringState.ACTIVE:
            mode = ServiceMode.ACTIVE
        elif open_episode_ids:
            mode = ServiceMode.FOLLOW_UP
        else:
            mode = ServiceMode.DORMANT
        return ServiceModeProjection(
            data_mode=namespace.data_mode,
            subject_id=subject_id,
            mode=mode,
            monitoring_state=monitoring_state,
            active_night_episode_id=(
                None
                if monitoring is None
                else monitoring.active_night_episode_id
            ),
            open_followup_night_episode_ids=open_episode_ids,
            projected_at=projected_at,
        )


__all__ = [
    "CareFollowupAccessPolicy",
    "CareFollowupAuthorizationError",
    "CareFollowupError",
    "CareFollowupService",
    "CareFollowupTransitionResult",
    "InvalidCareFollowupTransitionError",
    "ServiceModeProjector",
]
