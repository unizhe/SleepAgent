from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sleepagent.sleep_api.auth import (
    AuthoritativeRoleBinding,
    FailClosedRoleBindingResolver,
)
from sleepagent.sleep_api.contracts import PublicActorRole

from tests.integration.test_sleep_api_v1 import (
    ROLE_SCOPES,
    _actor_id,
    _authorization_id,
    _client,
    _environment,
    _headers,
)


EVENT_PATH = "/api/v1/subjects/elder-1/events"


def _poll(environment, *, role, scopes, jti, cursor=None, limit=1):
    query = {"limit": limit}
    if cursor is not None:
        query["cursor"] = cursor
    return _client(environment).get(
        EVENT_PATH,
        params=query,
        headers=_headers(
            environment,
            method="GET",
            path=EVENT_PATH,
            role=role,
            scopes=scopes,
            jti=jti,
        ),
    )


def test_event_projection_is_minimized_scoped_and_has_no_global_offset(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "events.sqlite3", seed=True)
    denied = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=ROLE_SCOPES[PublicActorRole.ELDER] - {"sleep:events:read"},
        jti="events-denied",
    )
    assert denied.status_code == 403

    doctor_scopes = ROLE_SCOPES[PublicActorRole.DOCTOR]
    response = _poll(
        environment,
        role=PublicActorRole.DOCTOR,
        scopes=doctor_scopes,
        jti="events-doctor",
        limit=100,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["delivery_semantics"] == "at_least_once"
    assert (
        payload["ordering_semantics"]
        == "per_aggregate_sequence_only_no_global_business_order"
    )
    encoded = response.text.lower()
    for forbidden in (
        "delivery_offset",
        "aggregate_type",
        "correlation_id",
        "causation_id",
        "agent",
        "authorization_id",
        "actor_id",
    ):
        assert forbidden not in encoded
    lifecycle_types = {
        "MONITORING_ACTIVATED",
        "MONITORING_DORMANT",
        "ELDER_IN_BED",
    }
    assert not lifecycle_types.intersection(
        event["event_type"] for event in payload["events"]
    )
    assert all(
        event["schema_version"] == "sleep_domain_event.v1"
        for event in payload["events"]
    )


def test_old_cursor_replays_same_page_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    environment = _environment(database, seed=True)
    scopes = ROLE_SCOPES[PublicActorRole.FAMILY]
    first = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="cursor-first",
        limit=1,
    )
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    second = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="cursor-second",
        cursor=cursor,
        limit=1,
    )
    duplicate = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="cursor-duplicate",
        cursor=cursor,
        limit=1,
    )
    assert second.json()["events"] == duplicate.json()["events"]

    restarted = _environment(
        database,
        seed=False,
        clock=environment.clock,
        authority=environment.authority,
        private_keys=environment.private_keys,
    )
    after_restart = _poll(
        restarted,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="cursor-after-restart",
        cursor=cursor,
        limit=1,
    )
    assert after_restart.status_code == 200
    assert after_restart.json()["events"] == second.json()["events"]

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    rejected = _poll(
        restarted,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="cursor-tampered",
        cursor=tampered,
        limit=1,
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "CURSOR_RESYNC_REQUIRED"


def test_cursor_expiry_schema_change_and_scope_change_require_resync(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "resync.sqlite3", seed=True)
    environment.runtime = replace(
        environment.runtime,
        event_cursor_ttl=timedelta(minutes=1),
    )
    scopes = ROLE_SCOPES[PublicActorRole.ELDER]
    initial = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="expiry-initial",
    )
    cursor = initial.json()["next_cursor"]
    environment.clock.advance(timedelta(minutes=2))
    expired = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="expiry-old",
        cursor=cursor,
    )
    assert expired.status_code == 409
    assert expired.json()["code"] == "CURSOR_RESYNC_REQUIRED"
    assert expired.json()["tombstone"] is None

    current = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="schema-initial",
    )
    environment.runtime = replace(
        environment.runtime,
        event_schema_generation="sleep-domain-events-v2",
    )
    schema_changed = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="schema-old",
        cursor=current.json()["next_cursor"],
    )
    assert schema_changed.status_code == 409

    projection = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="projection-initial",
    )
    reduced_scopes = frozenset({"sleep:events:read", "sleep:episode:read"})
    reduced = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=reduced_scopes,
        jti="projection-reduced",
        cursor=projection.json()["next_cursor"],
    )
    assert reduced.status_code == 409
    assert (
        reduced.json()["details"]["reason"]
        == "authorization_projection_changed"
    )
    assert reduced.json()["tombstone"] is not None


def test_revocation_denies_future_reads_and_returns_stable_tombstone(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "revocation.sqlite3", seed=True)
    scopes = ROLE_SCOPES[PublicActorRole.FAMILY]
    initial = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="revoke-initial",
    )
    cursor = initial.json()["next_cursor"]
    environment.authority.put(
        AuthoritativeRoleBinding(
            authorization_id=_authorization_id(PublicActorRole.FAMILY),
            actor_id=_actor_id(PublicActorRole.FAMILY),
            subject_id="elder-1",
            role=PublicActorRole.FAMILY,
            scopes=scopes,
            authorization_epoch=2,
            active=False,
        )
    )
    revoked = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="revoke-old",
        cursor=cursor,
    )
    assert revoked.status_code == 409
    error = revoked.json()
    assert error["code"] == "CURSOR_RESYNC_REQUIRED"
    assert error["tombstone"]["event_type"] == "ACCESS_REVOKED_TOMBSTONE"
    assert error["tombstone"]["action_required"] == "delete_local_authorized_cache"
    assert error["tombstone"]["remote_recall_possible"] is False

    repeated = _poll(
        environment,
        role=PublicActorRole.FAMILY,
        scopes=scopes,
        jti="revoke-repeat",
        cursor=cursor,
    )
    assert repeated.status_code == 409
    assert repeated.json()["details"]["reason"] == "cursor_revoked"
    assert repeated.json()["tombstone"] == error["tombstone"]


def test_authority_outage_fails_closed_without_fabricating_revocation(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "authority.sqlite3", seed=True)
    scopes = ROLE_SCOPES[PublicActorRole.ELDER]
    initial = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="authority-initial",
    )
    environment.runtime.authenticator.role_binding_resolver = (
        FailClosedRoleBindingResolver()
    )
    unavailable = _poll(
        environment,
        role=PublicActorRole.ELDER,
        scopes=scopes,
        jti="authority-unavailable",
        cursor=initial.json()["next_cursor"],
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "AUTHORIZATION_UNAVAILABLE"
    assert unavailable.json()["tombstone"] is None
