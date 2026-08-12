"""Deployment configuration for the independently authenticated sleep API."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sleepagent.persistence import (
    RadarPersistenceStore,
    connect_postgres_store,
)
from sleepagent.sleep_api.auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    AuthoritativeRoleBinding,
    HttpsBearerServicePrincipalVerifier,
    InMemoryAuthoritativeRoleBindingResolver,
    RotatingServiceCredential,
    SleepApiAuthenticator,
)
from sleepagent.sleep_api.contracts import PublicActorRole
from sleepagent.sleep_api.persistence import SleepApiPersistence
from sleepagent.sleep_api.service import (
    AuthorizationEpochRoleViewCache,
    OpaquePageCursorCodec,
    SleepApiRuntime,
)
from sleepagent.sleep_domain import (
    CompatibilityMigrationController,
    DataMode,
    DataAuthority,
    DomainNamespace,
    EpisodeBoundaryPolicy,
    EpisodeVersionPins,
    LifecycleAccessPolicy,
    LifecycleTransitionPolicy,
    NightEpisodeService,
    RawPayloadEncryptionPolicy,
    SleepDomainRepository,
)
from sleepagent.sleep_domain.authority_migration import READ_AUTHORITY_ENV


UTC = timezone.utc
SLEEP_API_MODE_ENV = "SLEEPAGENT_SLEEP_API_MODE"
SLEEP_API_SERVICE_CREDENTIALS_ENV = "SLEEPAGENT_SLEEP_API_SERVICE_CREDENTIALS_JSON"
SLEEP_API_ACTOR_KEYS_ENV = "SLEEPAGENT_SLEEP_API_ACTOR_KEYS_JSON"
SLEEP_API_ROLE_BINDINGS_ENV = "SLEEPAGENT_SLEEP_API_ROLE_BINDINGS_JSON"
SLEEP_API_AUDIENCE_ENV = "SLEEPAGENT_SLEEP_API_AUDIENCE"
SLEEP_API_CURSOR_SECRET_ENV = "SLEEPAGENT_SLEEP_API_CURSOR_SECRET"
SLEEP_API_EVENT_CURSOR_TTL_SECONDS_ENV = (
    "SLEEPAGENT_SLEEP_API_EVENT_CURSOR_TTL_SECONDS"
)
SLEEP_API_EVENT_SCHEMA_GENERATION_ENV = (
    "SLEEPAGENT_SLEEP_API_EVENT_SCHEMA_GENERATION"
)
SLEEP_API_NAMESPACE_ENV = "SLEEPAGENT_SLEEP_API_NAMESPACE"
SLEEP_API_RAW_KEY_ENV = "SLEEPAGENT_SLEEP_API_RAW_FERNET_KEY"
SLEEP_API_VERSION_PINS_ENV = "SLEEPAGENT_SLEEP_API_VERSION_PINS_JSON"
RADAR_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"
RADAR_SQLITE_PATH_ENV = "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH"
DEFAULT_SLEEP_API_SQLITE_PATH = "/tmp/sleepagent_sleep_api.sqlite3"

def build_sleep_api_runtime_from_env(
    *,
    environment: Mapping[str, str] | None = None,
    store: RadarPersistenceStore | None = None,
) -> SleepApiRuntime:
    env = dict(os.environ if environment is None else environment)
    mode = env.get(SLEEP_API_MODE_ENV, "production").strip().lower()
    if mode not in {"production", "development", "test"}:
        raise ValueError("SLEEPAGENT_SLEEP_API_MODE is invalid")
    production = mode == "production"
    persistence_store = store or _store_from_env(env, production=production)
    if production and persistence_store.dialect != "postgres":
        raise ValueError(
            "production sleep API requires shared PostgreSQL durable storage"
        )
    if production:
        raw_policy = RawPayloadEncryptionPolicy.from_environment(
            production=True,
            environ=env,
        )
    else:
        configured_raw_names = {
            "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY",
            "SLEEP_DOMAIN_RAW_ENCRYPTION_KEY_ID",
            "SLEEP_DOMAIN_RAW_RETENTION_SECONDS",
        }
        if configured_raw_names.issubset(env):
            raw_policy = RawPayloadEncryptionPolicy.from_environment(
                production=False,
                environ=env,
            )
        else:
            raw_key = env.get(SLEEP_API_RAW_KEY_ENV) or base64.urlsafe_b64encode(
                os.urandom(32)
            ).decode("ascii")
            raw_policy = RawPayloadEncryptionPolicy(
                key_id="sleep-api-runtime-local-v1",
                key=raw_key.encode("ascii"),
                retention_period=_retention_for_mode(False),
                production=False,
            )
    repository = SleepDomainRepository(
        persistence_store,
        raw_payload_policy=raw_policy,
    )
    api_persistence = SleepApiPersistence(persistence_store)
    credentials = tuple(
        _service_credential(item)
        for item in _json_list(env, SLEEP_API_SERVICE_CREDENTIALS_ENV)
    )
    actor_keys = tuple(
        _actor_key(item) for item in _json_list(env, SLEEP_API_ACTOR_KEYS_ENV)
    )
    bindings = tuple(
        _role_binding(item)
        for item in _json_list(env, SLEEP_API_ROLE_BINDINGS_ENV)
    )
    if not credentials or not actor_keys or not bindings:
        raise ValueError(
            "service credentials, actor keys and authoritative role bindings "
            "must all be configured"
        )
    audience = env.get(SLEEP_API_AUDIENCE_ENV, "").strip()
    cursor_secret = env.get(SLEEP_API_CURSOR_SECRET_ENV, "")
    if not audience or len(cursor_secret.encode("utf-8")) < 32:
        raise ValueError("actor audience and a 32-byte cursor secret are required")
    authority = InMemoryAuthoritativeRoleBindingResolver(bindings)
    authenticator = SleepApiAuthenticator(
        service_verifier=HttpsBearerServicePrincipalVerifier(
            credentials,
            require_https=production,
        ),
        actor_verifier=ActorAssertionVerifier(
            actor_keys,
            audience=audience,
            replay_store=api_persistence,
        ),
        role_binding_resolver=authority,
    )
    namespace_id = env.get(SLEEP_API_NAMESPACE_ENV, "live:default").strip()
    data_mode = (
        DataMode.REPLAY if namespace_id.startswith("replay:") else DataMode.LIVE
    )
    if production and (
        data_mode != DataMode.LIVE or not namespace_id.startswith("live:")
    ):
        raise ValueError("production sleep API requires a live namespace")
    namespace = DomainNamespace(namespace_id, data_mode)
    if production:
        if env.get(READ_AUTHORITY_ENV, "").strip().lower() != "canonical":
            raise ValueError(
                "production sleep API requires "
                f"{READ_AUTHORITY_ENV}=canonical"
            )
        cutover = CompatibilityMigrationController(repository).get_state(
            namespace
        )
        if (
            cutover is None
            or cutover.selected_authority != DataAuthority.CANONICAL
        ):
            raise ValueError(
                "production canonical authority has not completed the "
                "configuration-gated cutover"
            )
    pins = _version_pins(env, production=production)
    authorization_grants: dict[str, frozenset[str]] = {}
    for binding in bindings:
        authorization_grants[binding.actor_id] = frozenset(
            {
                *authorization_grants.get(binding.actor_id, frozenset()),
                binding.authorization_id,
            }
        )
    boundary = EpisodeBoundaryPolicy(policy_version="sleep-api-boundary.v1")
    transition = LifecycleTransitionPolicy(
        policy_version="sleep-api-lifecycle.v1"
    )
    episode_service = NightEpisodeService(
        repository,
        boundary_policies={boundary.policy_version: boundary},
        active_boundary_policy_version=boundary.policy_version,
        transition_policy=transition,
        access_policy=LifecycleAccessPolicy(authorization_grants),
        default_version_pins=pins,
        worker_id="sleep-api-lifecycle",
    )
    return SleepApiRuntime(
        namespace=namespace,
        repository=repository,
        api_persistence=api_persistence,
        episode_service=episode_service,
        authority=authority,
        cursor_codec=OpaquePageCursorCodec(cursor_secret.encode("utf-8")),
        role_view_cache=AuthorizationEpochRoleViewCache(),
        authenticator=authenticator,
        event_cursor_ttl=_event_cursor_ttl(env),
        event_schema_generation=env.get(
            SLEEP_API_EVENT_SCHEMA_GENERATION_ENV,
            "sleep-domain-events-v1",
        ).strip(),
    )


def _store_from_env(
    env: Mapping[str, str],
    *,
    production: bool,
) -> RadarPersistenceStore:
    database_url = env.get(RADAR_DATABASE_URL_ENV, "").strip()
    if database_url:
        lowered = database_url.lower()
        if production and (
            lowered.startswith("sqlite")
            or ":memory:" in lowered
            or "/tmp/" in lowered
        ):
            raise ValueError(
                "production sleep API requires shared PostgreSQL durable storage"
            )
        return connect_postgres_store(database_url)
    if production:
        raise ValueError("production sleep API requires PostgreSQL durable storage")
    sqlite_path = Path(
        env.get(RADAR_SQLITE_PATH_ENV, DEFAULT_SLEEP_API_SQLITE_PATH)
    )
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(sqlite_path, check_same_thread=False, timeout=30)
    )


def _service_credential(item: Mapping[str, Any]) -> RotatingServiceCredential:
    return RotatingServiceCredential.from_secret(
        credential_id=str(item["credential_id"]),
        principal_id=str(item["principal_id"]),
        secret=str(item["secret"]),
        not_before=_datetime(item["not_before"]),
        not_after=_datetime(item["not_after"]),
        allowed_actor_issuers=frozenset(
            str(value) for value in item.get("allowed_actor_issuers", [])
        ),
    )


def _actor_key(item: Mapping[str, Any]) -> ActorVerificationKey:
    return ActorVerificationKey(
        issuer=str(item["issuer"]),
        key_id=str(item["key_id"]),
        algorithm=str(item["algorithm"]),
        public_key_pem=str(item["public_key_pem"]).encode("utf-8"),
        not_before=_datetime(item["not_before"]),
        not_after=_datetime(item["not_after"]),
    )


def _role_binding(item: Mapping[str, Any]) -> AuthoritativeRoleBinding:
    expires_at = item.get("expires_at")
    return AuthoritativeRoleBinding(
        authorization_id=str(item["authorization_id"]),
        actor_id=str(item["actor_id"]),
        subject_id=str(item["subject_id"]),
        role=PublicActorRole(str(item["role"])),
        scopes=frozenset(str(value) for value in item["scopes"]),
        authorization_epoch=int(item["authorization_epoch"]),
        active=bool(item.get("active", True)),
        expires_at=None if expires_at is None else _datetime(expires_at),
    )


def _version_pins(
    env: Mapping[str, str],
    *,
    production: bool,
) -> EpisodeVersionPins:
    raw = env.get(SLEEP_API_VERSION_PINS_ENV)
    if raw is None:
        if production:
            raise ValueError("production sleep API requires version pins")
        payload = {
            "adapter_versions": {"acceptance": "fixture.v1"},
            "observation_schema_versions": ["sleep_observation.v1"],
            "policy_versions": {
                "episode_boundary": "sleep-api-boundary.v1",
                "lifecycle_transition": "sleep-api-lifecycle.v1",
            },
        }
    else:
        payload = json.loads(raw)
    return EpisodeVersionPins(
        adapter_versions={
            str(key): str(value)
            for key, value in payload["adapter_versions"].items()
        },
        observation_schema_versions=tuple(
            str(value) for value in payload["observation_schema_versions"]
        ),
        policy_versions={
            str(key): str(value)
            for key, value in payload["policy_versions"].items()
        },
    )


def _json_list(env: Mapping[str, str], key: str) -> list[Mapping[str, Any]]:
    raw = env.get(key)
    if raw is None:
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a JSON array of objects")
    return value


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("configuration timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _retention_for_mode(production: bool):
    from datetime import timedelta

    return timedelta(days=30 if production else 1)


def _event_cursor_ttl(env: Mapping[str, str]):
    from datetime import timedelta

    seconds = int(env.get(SLEEP_API_EVENT_CURSOR_TTL_SECONDS_ENV, "86400"))
    if seconds < 60 or seconds > 604800:
        raise ValueError("event cursor TTL must be between 60 and 604800 seconds")
    return timedelta(seconds=seconds)


__all__ = [
    "build_sleep_api_runtime_from_env",
]
