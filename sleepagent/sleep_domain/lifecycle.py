"""Deterministic Monitoring and NightEpisode lifecycle decisions.

This module contains no scheduler, Agent, LLM, provider transport, or medical
policy. Persistence and outbox commits are owned by ``SleepDomainRepository``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from sleepagent.sleep_domain.contracts import (
    LifecycleTransitionPolicy,
    LifecycleTrigger,
    LifecycleTriggerKind,
    LifecycleTriggerSource,
    ManualMonitoringOverride,
    MonitoringSnapshot,
    MonitoringState,
    NightEpisodeState,
)


class LifecycleError(RuntimeError):
    pass


class LifecycleAuthorizationError(LifecycleError):
    pass


class LifecycleBusyError(LifecycleError):
    pass


class InvalidLifecycleTransitionError(LifecycleError):
    pass


@dataclass(frozen=True)
class LifecycleAccessPolicy:
    """Deployment-owned grants for internal authorized lifecycle commands."""

    grants: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "grants",
            MappingProxyType(
                {
                    str(actor_id): frozenset(str(item) for item in authorization_ids)
                    for actor_id, authorization_ids in self.grants.items()
                }
            ),
        )

    def authorize(self, trigger: LifecycleTrigger) -> None:
        if trigger.source not in {
            LifecycleTriggerSource.AUTHORIZED_COMMAND,
            LifecycleTriggerSource.EXPLICIT_REANALYSIS,
        }:
            return
        allowed = self.grants.get(str(trigger.actor_id), frozenset())
        if trigger.authorization_id not in allowed:
            raise LifecycleAuthorizationError(
                "lifecycle command authorization was not granted by deployment policy"
            )


@dataclass(frozen=True)
class MonitoringDecision:
    snapshot: MonitoringSnapshot
    state_changed: bool
    snapshot_changed: bool
    reason_code: str
    suppressed: bool


_PRIORITY = {
    LifecycleTriggerSource.TIME_FALLBACK: 10,
    LifecycleTriggerSource.RESTART_RECOVERY: 10,
    LifecycleTriggerSource.VERIFIED_DEVICE_EVENT: 20,
    LifecycleTriggerSource.REPORT_DEADLINE: 20,
    LifecycleTriggerSource.SOURCE_REPORT: 20,
    LifecycleTriggerSource.AUTHORIZED_COMMAND: 30,
    LifecycleTriggerSource.EXPLICIT_REANALYSIS: 30,
}


def trigger_priority(trigger: LifecycleTrigger) -> int:
    return _PRIORITY[trigger.source]


def monitoring_target(trigger: LifecycleTrigger) -> MonitoringState | None:
    if trigger.kind in {
        LifecycleTriggerKind.IN_BED,
        LifecycleTriggerKind.ACTIVATE,
        LifecycleTriggerKind.FALLBACK_START,
    }:
        return MonitoringState.ACTIVE
    if trigger.kind in {
        LifecycleTriggerKind.OUT_OF_BED,
        LifecycleTriggerKind.DEACTIVATE,
        LifecycleTriggerKind.FALLBACK_END,
    }:
        return MonitoringState.DORMANT
    return None


def decide_monitoring_transition(
    snapshot: MonitoringSnapshot,
    trigger: LifecycleTrigger,
    policy: LifecycleTransitionPolicy,
    *,
    active_night_episode_id: str | None,
) -> MonitoringDecision:
    """Apply priority, debounce, dwell and expiring manual-override rules."""

    if snapshot.subject_id != trigger.subject_id:
        raise ValueError("monitoring snapshot and trigger subject do not match")
    if snapshot.data_mode != trigger.data_mode:
        raise ValueError("monitoring snapshot and trigger data_mode do not match")
    if snapshot.last_trigger_id == trigger.trigger_id:
        return MonitoringDecision(
            snapshot=snapshot,
            state_changed=False,
            snapshot_changed=False,
            reason_code="duplicate_trigger",
            suppressed=True,
        )

    target = monitoring_target(trigger)
    if target is None:
        return MonitoringDecision(
            snapshot=snapshot,
            state_changed=False,
            snapshot_changed=False,
            reason_code="trigger_does_not_target_monitoring",
            suppressed=False,
        )

    priority = trigger_priority(trigger)
    manual_override = snapshot.manual_override
    if (
        manual_override is not None
        and manual_override.expires_at <= trigger.occurred_at
    ):
        manual_override = None
    is_manual = trigger.source == LifecycleTriggerSource.AUTHORIZED_COMMAND

    if (
        manual_override is not None
        and not is_manual
        and target != manual_override.state
    ):
        return MonitoringDecision(
            snapshot=snapshot,
            state_changed=False,
            snapshot_changed=False,
            reason_code="manual_override_active",
            suppressed=True,
        )

    if (
        snapshot.debounce_until is not None
        and trigger.occurred_at < snapshot.debounce_until
        and priority <= snapshot.last_trigger_priority
        and not is_manual
    ):
        return MonitoringDecision(
            snapshot=snapshot,
            state_changed=False,
            snapshot_changed=False,
            reason_code="debounced_lower_or_equal_priority",
            suppressed=True,
        )

    next_override = manual_override
    if is_manual:
        next_override = ManualMonitoringOverride(
            state=target,
            actor_id=str(trigger.actor_id),
            authorization_id=str(trigger.authorization_id),
            set_at=trigger.occurred_at,
            expires_at=trigger.occurred_at
            + timedelta(seconds=policy.manual_override_seconds),
        )

    if target == snapshot.state:
        updated = snapshot.model_copy(
            update={
                "last_trigger_id": trigger.trigger_id,
                "last_trigger_source": trigger.source,
                "last_trigger_priority": priority,
                "debounce_until": trigger.occurred_at
                + timedelta(seconds=policy.debounce_seconds),
                "manual_override": next_override,
                "active_night_episode_id": (
                    active_night_episode_id
                    if target == MonitoringState.ACTIVE
                    else None
                ),
                "cas_version": snapshot.cas_version + 1,
                "updated_at": trigger.received_at,
            }
        )
        return MonitoringDecision(
            snapshot=updated,
            state_changed=False,
            snapshot_changed=updated != snapshot,
            reason_code="idempotent_target_state",
            suppressed=False,
        )

    dwell_seconds = (
        policy.minimum_active_dwell_seconds
        if snapshot.state == MonitoringState.ACTIVE
        else policy.minimum_dormant_dwell_seconds
    )
    if (
        not is_manual
        and trigger.occurred_at
        < snapshot.state_entered_at + timedelta(seconds=dwell_seconds)
    ):
        return MonitoringDecision(
            snapshot=snapshot,
            state_changed=False,
            snapshot_changed=False,
            reason_code="minimum_dwell_not_met",
            suppressed=True,
        )

    updated = snapshot.model_copy(
        update={
            "state": target,
            "state_entered_at": trigger.occurred_at,
            "last_trigger_id": trigger.trigger_id,
            "last_trigger_source": trigger.source,
            "last_trigger_priority": priority,
            "debounce_until": trigger.occurred_at
            + timedelta(seconds=policy.debounce_seconds),
            "manual_override": next_override,
            "active_night_episode_id": (
                active_night_episode_id
                if target == MonitoringState.ACTIVE
                else None
            ),
            "cas_version": snapshot.cas_version + 1,
            "updated_at": trigger.received_at,
        }
    )
    return MonitoringDecision(
        snapshot=updated,
        state_changed=True,
        snapshot_changed=True,
        reason_code=(
            "authorized_manual_transition"
            if is_manual
            else "deterministic_trigger_transition"
        ),
        suppressed=False,
    )


_ALLOWED_EPISODE_TRANSITIONS = frozenset(
    {
        (NightEpisodeState.COLLECTING, NightEpisodeState.AWAITING_REPORT),
        (NightEpisodeState.AWAITING_REPORT, NightEpisodeState.ANALYZED),
        (NightEpisodeState.ANALYZED, NightEpisodeState.CLOSED),
        (NightEpisodeState.ANALYZED, NightEpisodeState.REVISED),
        (NightEpisodeState.CLOSED, NightEpisodeState.REVISED),
        (NightEpisodeState.REVISED, NightEpisodeState.CLOSED),
    }
)


def require_night_episode_transition(
    from_state: NightEpisodeState,
    to_state: NightEpisodeState,
) -> None:
    if (from_state, to_state) not in _ALLOWED_EPISODE_TRANSITIONS:
        raise InvalidLifecycleTransitionError(
            f"invalid NightEpisode transition: {from_state.value} -> "
            f"{to_state.value}"
        )


__all__ = [
    "InvalidLifecycleTransitionError",
    "LifecycleAccessPolicy",
    "LifecycleAuthorizationError",
    "LifecycleBusyError",
    "LifecycleError",
    "MonitoringDecision",
    "decide_monitoring_transition",
    "monitoring_target",
    "require_night_episode_transition",
    "trigger_priority",
]
