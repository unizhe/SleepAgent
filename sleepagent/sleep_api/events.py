"""External, role-minimized projections of committed domain outbox events."""

from __future__ import annotations

from dataclasses import dataclass

from sleepagent.sleep_api.contracts import ExternalDomainEvent, PublicActorRole
from sleepagent.sleep_domain import DomainEvent, DomainEventType


_PUBLIC_EVENT_NAMES = {
    DomainEventType.AGENT_ANALYSIS_READY: "SLEEP_VIEW_READY",
    DomainEventType.AGENT_ANALYSIS_DEGRADED: "SLEEP_VIEW_DEGRADED",
}
_RISK_EVENTS = {
    DomainEventType.RISK_SIGNAL_DETECTED,
    DomainEventType.RISK_STATE_CHANGED,
    DomainEventType.DOCTOR_CONTACT_SUGGESTED,
}
_LIFECYCLE_EVENTS = {
    DomainEventType.MONITORING_ACTIVATED,
    DomainEventType.MONITORING_DORMANT,
    DomainEventType.ELDER_IN_BED,
    DomainEventType.BED_EXIT_DETECTED,
    DomainEventType.BED_STATE_CHANGED,
    DomainEventType.DEVICE_OFFLINE,
    DomainEventType.DEVICE_ONLINE,
    DomainEventType.DEVICE_BINDING_REQUIRED,
}
_FAMILY_ONLY = {
    DomainEventType.FAMILY_ATTENTION_SUGGESTED,
}
_HOUSEHOLD_ROLES = {
    PublicActorRole.ELDER,
    PublicActorRole.FAMILY,
    PublicActorRole.CAREGIVER,
}
_SAFE_ATTRIBUTE_KEYS = {
    "state",
    "data_sufficiency",
    "revision_number",
    "revision_cause",
    "policy_version",
    "signal_type",
    "reminder",
    "suppressed_repeat_count",
    "from_state",
    "to_state",
    "reason_code",
    "report_deadline_at",
    "lateness_watermark_at",
    "timezone_name",
    "derivation",
}


@dataclass(frozen=True)
class EventProjectionContext:
    role: PublicActorRole
    scopes: frozenset[str]


def project_domain_event(
    event: DomainEvent,
    context: EventProjectionContext,
) -> ExternalDomainEvent | None:
    """Return the public projection, or ``None`` when this view cannot see it."""

    required_scope = (
        "sleep:risk:read"
        if event.event_type in _RISK_EVENTS
        else (
            "sleep:lifecycle:read"
            if event.event_type in _LIFECYCLE_EVENTS
            else "sleep:episode:read"
        )
    )
    if required_scope not in context.scopes:
        return None
    if event.event_type in _LIFECYCLE_EVENTS and context.role not in _HOUSEHOLD_ROLES:
        return None
    if (
        event.event_type in _FAMILY_ONLY
        and context.role not in {PublicActorRole.FAMILY, PublicActorRole.CAREGIVER}
    ):
        return None

    if event.event_type in {
        DomainEventType.AGENT_ANALYSIS_READY,
        DomainEventType.AGENT_ANALYSIS_DEGRADED,
    }:
        payload = {
            "view_status": (
                "ready"
                if event.event_type == DomainEventType.AGENT_ANALYSIS_READY
                else "degraded"
            )
        }
    else:
        payload = {
            key: value
            for key, value in event.attributes.items()
            if key in _SAFE_ATTRIBUTE_KEYS
        }
    return ExternalDomainEvent(
        event_id=event.event_id,
        event_type=_PUBLIC_EVENT_NAMES.get(event.event_type, event.event_type.value),
        event_version=event.event_version,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        per_aggregate_sequence=event.per_aggregate_sequence,
        subject_id=event.subject_id,
        night_episode_id=event.night_episode_id,
        night_episode_revision_id=event.night_episode_revision_id,
        # Operation ownership is not inferable from a domain event alone. Omit it
        # rather than leak another actor's command identifier.
        operation_id=None,
        occurred_at=event.event_occurred_at,
        persisted_at=event.persisted_at,
        payload=payload,
    )


__all__ = ["EventProjectionContext", "project_domain_event"]
