from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from sleepagent.radar_agent.persistence import (
    MIGRATION_VERSION,
    RADAR_AGENT_POSTGRES_MIGRATIONS,
    RadarPersistenceStore,
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
from sleepagent.sleep_api.app import create_sleep_api_app
from sleepagent.sleep_api.contracts import PublicActorRole
from sleepagent.sleep_api.persistence import SleepApiPersistence
from sleepagent.sleep_api.runtime import build_sleep_api_runtime_from_env
from sleepagent.sleep_api.service import (
    AuthorizationEpochRoleViewCache,
    OpaquePageCursorCodec,
    SleepApiOperationWorker,
    SleepApiRuntime,
)
from sleepagent.sleep_domain import (
    AlgorithmVersionValue,
    AnalysisRevision,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisStatus,
    CalibrationValue,
    CollectionWindowDerivation,
    ConfidenceValue,
    CurrentRisk,
    DataMode,
    DataSufficiency,
    DeterministicQualityAssessment,
    DeterministicSourceScope,
    DeviceBinding,
    DeviceBindingStatus,
    DomainNamespace,
    EpisodeBoundaryPolicy,
    EpisodeVersionPins,
    LifecycleAccessPolicy,
    LifecycleTransitionPolicy,
    LifecycleTrigger,
    LifecycleTriggerKind,
    LifecycleTriggerSource,
    MissingnessState,
    NightEpisodeService,
    ObservationType,
    ProviderAccountRecord,
    ProviderDeviceIdentity,
    QualityState,
    RawPayloadEncryptionPolicy,
    RiskState,
    RoleViewStatus,
    SleepDomainRepository,
)


UTC = timezone.utc
START = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
NAMESPACE = DomainNamespace("live:sleep-api-tests", DataMode.LIVE)
AUDIENCE = "sleepagent-api-v1"
ISSUER = "养老-os"
SERVICE_OLD = "service-old-secret"
SERVICE_NEW = "service-new-secret"
ALL_READ = {
    "sleep:lifecycle:read",
    "sleep:risk:read",
    "sleep:episode:read",
    "sleep:operation:read",
    "sleep:events:read",
}
ROLE_SCOPES = {
    PublicActorRole.ELDER: frozenset(
        {
            *ALL_READ,
            "sleep:view:elder",
            "sleep:monitoring:write",
            "sleep:feedback:self",
            "sleep:reanalysis:write",
        }
    ),
    PublicActorRole.FAMILY: frozenset(
        {
            *ALL_READ,
            "sleep:view:family",
            "sleep:monitoring:write",
            "sleep:feedback:family",
            "sleep:reanalysis:write",
        }
    ),
    PublicActorRole.CAREGIVER: frozenset(
        {
            *ALL_READ,
            "sleep:view:family",
            "sleep:monitoring:write",
            "sleep:feedback:family",
            "sleep:reanalysis:write",
        }
    ),
    PublicActorRole.DOCTOR: frozenset(
        {
            *ALL_READ,
            "sleep:view:doctor",
            "sleep:reanalysis:write",
        }
    ),
}


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class ApiEnvironment:
    runtime: SleepApiRuntime
    authority: InMemoryAuthoritativeRoleBindingResolver
    clock: Clock
    private_keys: dict[str, ed25519.Ed25519PrivateKey]
    bindings: tuple[AuthoritativeRoleBinding, ...]
    store: RadarPersistenceStore
    episode_id: str | None = None
    revision_id: str | None = None

    def app(self) -> FastAPI:
        return create_sleep_api_app(
            lambda: self.runtime,
            manage_worker=False,
        )


def _actor_id(role: PublicActorRole) -> str:
    return f"{role.value}-actor"


def _authorization_id(role: PublicActorRole) -> str:
    return f"auth-{role.value}"


def _bindings(*, epoch: int = 1) -> tuple[AuthoritativeRoleBinding, ...]:
    return tuple(
        AuthoritativeRoleBinding(
            authorization_id=_authorization_id(role),
            actor_id=_actor_id(role),
            subject_id="elder-1",
            role=role,
            scopes=ROLE_SCOPES[role],
            authorization_epoch=epoch,
        )
        for role in PublicActorRole
    )


def _environment(
    database_path: Path,
    *,
    seed: bool,
    clock: Clock | None = None,
    authority: InMemoryAuthoritativeRoleBindingResolver | None = None,
    private_keys: dict[str, ed25519.Ed25519PrivateKey] | None = None,
) -> ApiEnvironment:
    clock = clock or Clock(START)
    connection = sqlite3.connect(
        database_path,
        check_same_thread=False,
        timeout=30,
    )
    store = RadarPersistenceStore.connect_sqlite(connection)
    repository = SleepDomainRepository(
        store,
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="tests",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=1),
            production=False,
        ),
    )
    api_persistence = SleepApiPersistence(store)
    bindings = _bindings()
    authority = authority or InMemoryAuthoritativeRoleBindingResolver(bindings)
    private_keys = private_keys or {
        "old": ed25519.Ed25519PrivateKey.generate(),
        "new": ed25519.Ed25519PrivateKey.generate(),
    }
    verification_keys = tuple(
        ActorVerificationKey(
            issuer=ISSUER,
            key_id=key_id,
            algorithm="EdDSA",
            public_key_pem=private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            not_before=START - timedelta(days=1),
            not_after=START + timedelta(days=10 if key_id == "old" else 30),
        )
        for key_id, private_key in private_keys.items()
    )
    credentials = (
        RotatingServiceCredential.from_secret(
            credential_id="service-old",
            principal_id="养老-os-service",
            secret=SERVICE_OLD,
            not_before=START - timedelta(days=1),
            not_after=START + timedelta(days=10),
            allowed_actor_issuers=frozenset({ISSUER}),
        ),
        RotatingServiceCredential.from_secret(
            credential_id="service-new",
            principal_id="养老-os-service",
            secret=SERVICE_NEW,
            not_before=START,
            not_after=START + timedelta(days=30),
            allowed_actor_issuers=frozenset({ISSUER}),
        ),
    )
    authenticator = SleepApiAuthenticator(
        service_verifier=HttpsBearerServicePrincipalVerifier(
            credentials,
            require_https=False,
        ),
        actor_verifier=ActorAssertionVerifier(
            verification_keys,
            audience=AUDIENCE,
            replay_store=api_persistence,
        ),
        role_binding_resolver=authority,
        now_factory=clock,
    )
    boundary = EpisodeBoundaryPolicy(
        policy_version="boundary.v1",
        report_deadline_local_minute=600,
    )
    transition = LifecycleTransitionPolicy(
        policy_version="lifecycle.v1",
        minimum_active_dwell_seconds=60,
        minimum_dormant_dwell_seconds=60,
        manual_override_seconds=3600,
    )
    episode_service = NightEpisodeService(
        repository,
        boundary_policies={boundary.policy_version: boundary},
        active_boundary_policy_version=boundary.policy_version,
        transition_policy=transition,
        access_policy=LifecycleAccessPolicy(
            {
                binding.actor_id: frozenset({binding.authorization_id})
                for binding in bindings
            }
        ),
        default_version_pins=EpisodeVersionPins(
            adapter_versions={"perceptor": "1.0.0"},
            observation_schema_versions=("sleep_observation.v1",),
            policy_versions={
                "episode_boundary": boundary.policy_version,
                "lifecycle_transition": transition.policy_version,
            },
        ),
        worker_id="test-lifecycle",
    )
    runtime = SleepApiRuntime(
        namespace=NAMESPACE,
        repository=repository,
        api_persistence=api_persistence,
        episode_service=episode_service,
        authority=authority,
        cursor_codec=OpaquePageCursorCodec(b"cursor-secret-for-tests-32-bytes!!"),
        role_view_cache=AuthorizationEpochRoleViewCache(),
        authenticator=authenticator,
        now_factory=clock,
    )
    environment = ApiEnvironment(
        runtime=runtime,
        authority=authority,
        clock=clock,
        private_keys=private_keys,
        bindings=bindings,
        store=store,
    )
    if seed:
        _seed_committed_projections(environment)
    else:
        episodes = repository.list_night_episodes(
            NAMESPACE,
            subject_id="elder-1",
        )
        if episodes:
            environment.episode_id = episodes[0].night_episode_id
            environment.revision_id = episodes[0].current_night_episode_revision_id
    return environment


def _seed_committed_projections(environment: ApiEnvironment) -> None:
    repository = environment.runtime.repository
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=NAMESPACE,
            provider_account_id="provider-account-1",
            provider_id="perceptor",
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"profile": "tests"},
            created_at=START - timedelta(days=1),
        )
    )
    repository.append_device_binding(
        NAMESPACE,
        DeviceBinding(
            data_mode=DataMode.LIVE,
            device_binding_id="binding-1",
            binding_version=1,
            device_id="device-1",
            provider_id="perceptor",
            provider_account_id="provider-account-1",
            provider_device=ProviderDeviceIdentity(
                provider_device_name="device-name-1"
            ),
            subject_id="elder-1",
            timezone_name="Asia/Shanghai",
            effective_from=START - timedelta(days=1),
            effective_until=START + timedelta(days=60),
            status=DeviceBindingStatus.ACTIVE,
            changed_by_actor_id="deployment-admin",
            change_reason="test binding",
            recorded_at=START - timedelta(days=1),
        ),
    )
    active = environment.runtime.episode_service.process_trigger(
        NAMESPACE,
        trigger=_lifecycle_trigger(
            kind=LifecycleTriggerKind.ACTIVATE,
            occurred_at=START,
            trigger_id="seed-activate",
        ),
        device_binding_id="binding-1",
    )
    assert active.episode is not None
    episode_id = active.episode.night_episode_id
    dormant_at = START + timedelta(hours=8)
    dormant = environment.runtime.episode_service.process_trigger(
        NAMESPACE,
        trigger=_lifecycle_trigger(
            kind=LifecycleTriggerKind.DEACTIVATE,
            occurred_at=dormant_at,
            trigger_id="seed-deactivate",
        ),
    )
    assert dormant.episode is not None
    awaiting = dormant.episode
    assert awaiting.report_deadline_at is not None
    published_at = awaiting.report_deadline_at + timedelta(seconds=1)
    published = environment.runtime.episode_service.publish_report_deadline(
        NAMESPACE,
        night_episode_id=episode_id,
        trigger_id="seed-report-deadline",
        published_at=published_at,
    )
    assert published.revision is not None
    revision = published.revision
    scope = DeterministicSourceScope(
        night_episode_id=episode_id,
        night_episode_revision_id=revision.night_episode_revision_id,
        observation_ids=(),
        observation_types=(),
        device_binding_ids=("binding-1",),
        window_start_at=START,
        window_end_at=dormant_at,
    )
    quality = DeterministicQualityAssessment(
        assessment_id="quality-1",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        night_episode_id=episode_id,
        quality_state=QualityState.SUFFICIENT,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        missingness_state=MissingnessState.COMPLETE,
        coverage_ratio=1.0,
        expected_bin_count=1,
        covered_bin_count=1,
        explicit_missing_interval_count=0,
        invalid_observation_count=0,
        stale=False,
        offline=False,
        clock_invalid=False,
        latest_observed_at=dormant_at,
        source_scope=scope,
        policy_version="quality.v1",
        reason_codes=("fixture_committed",),
        assessed_at=published_at,
    )
    risk = CurrentRisk(
        current_risk_id="risk-1",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        night_episode_id=episode_id,
        risk_state=RiskState.NO_REVIEWED_SIGNAL,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        source_scope=scope,
        policy_version="risk.v1",
        observed_at=dormant_at,
        reason_codes=("no_reviewed_signal",),
        health_escalation_allowed=False,
        updated_at=published_at,
    )
    repository.commit_deterministic_fast_path(
        NAMESPACE,
        quality=quality,
        risk=risk,
        alert_instances=(),
        alert_receipts=(),
        signal_projections=(),
        signal_receipts=(),
        events=(),
    )
    analysis = AnalysisRevision(
        analysis_revision_id="analysis-1",
        night_episode_id=episode_id,
        night_episode_revision_id=revision.night_episode_revision_id,
        night_episode_revision_number=revision.revision_number,
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        revision_number=1,
        analysis_run_id="opaque-internal-run",
        observation_set_sha256=revision.observation_set_sha256,
        source_report_sha256=revision.source_report_sha256,
        adapter_versions={"perceptor": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"risk": "risk.v1"},
        data_sufficiency=revision.data_sufficiency,
        status=AnalysisStatus.READY,
        execution_mode="deterministic_only",
        result_resource_id="result-1",
        created_at=published_at + timedelta(seconds=1),
    )
    repository.append_analysis_revision(NAMESPACE, analysis)
    for role, content in (
        (AnalysisRole.ELDER, "老人版已提交晨间摘要。"),
        (AnalysisRole.FAMILY, "家属版已提交照护摘要。"),
        (AnalysisRole.DOCTOR, "医生版已提交结构化摘要。"),
    ):
        repository.append_analysis_role_view(
            NAMESPACE,
            AnalysisRoleView(
                role_view_id=f"view-{role.value}",
                analysis_revision_id=analysis.analysis_revision_id,
                night_episode_id=episode_id,
                night_episode_revision_id=revision.night_episode_revision_id,
                data_mode=DataMode.LIVE,
                subject_id="elder-1",
                role=role,
                status=RoleViewStatus.READY,
                product_agent_episode_id="opaque-internal-run",
                execution_mode="deterministic_only",
                content=content,
                generated_at=published_at + timedelta(seconds=1),
            ),
        )
    environment.episode_id = episode_id
    environment.revision_id = revision.night_episode_revision_id
    environment.clock.current = published_at + timedelta(days=1)


def _lifecycle_trigger(
    *,
    kind: LifecycleTriggerKind,
    occurred_at: datetime,
    trigger_id: str,
) -> LifecycleTrigger:
    return LifecycleTrigger(
        trigger_id=trigger_id,
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
        kind=kind,
        occurred_at=occurred_at,
        received_at=occurred_at,
        actor_id=_actor_id(PublicActorRole.ELDER),
        authorization_id=_authorization_id(PublicActorRole.ELDER),
        correlation_id=f"corr-{trigger_id}",
    )


def _body_bytes(body: dict[str, Any] | None) -> bytes:
    if body is None:
        return b""
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _headers(
    environment: ApiEnvironment,
    *,
    method: str,
    path: str,
    role: PublicActorRole,
    scopes: set[str] | frozenset[str],
    body: dict[str, Any] | None = None,
    jti: str,
    key_id: str = "new",
    service_secret: str = SERVICE_NEW,
    subject_id: str = "elder-1",
    actor_id: str | None = None,
    claim_updates: dict[str, Any] | None = None,
) -> dict[str, str]:
    now = environment.clock()
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": jti,
        "actor_id": actor_id or _actor_id(role),
        "subject_id": subject_id,
        "role": role.value,
        "scope": sorted(scopes),
        "iat": int(now.timestamp()) - 1,
        "exp": int((now + timedelta(minutes=2)).timestamp()),
        "nonce": f"nonce-{jti}-unique-value",
        "method": method,
        "path": path,
        "body_sha256": hashlib.sha256(_body_bytes(body)).hexdigest(),
    }
    claims.update(claim_updates or {})
    encoded_header = _b64url(
        json.dumps(
            {"alg": "EdDSA", "kid": key_id, "typ": "JWT"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    encoded_claims = _b64url(
        json.dumps(
            claims,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = environment.private_keys[key_id].sign(signing_input)
    return {
        "Authorization": f"Bearer {service_secret}",
        "X-Sleep-Actor-Assertion": (
            f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"
        ),
        "X-Correlation-ID": f"corr-{jti}",
    }


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _client(environment: ApiEnvironment) -> TestClient:
    return TestClient(environment.app())


def test_openapi_is_versioned_bounded_and_does_not_expose_internal_concepts(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "openapi.sqlite3", seed=True)
    schema = environment.app().openapi()
    paths = schema["paths"]
    assert "/api/v1/subjects/{subject_id}/lifecycle" in paths
    assert "/api/v1/subjects/{subject_id}/risk" in paths
    assert "/api/v1/operations/{operation_id}" in paths
    assert "/api/v1/subjects/{subject_id}/events" in paths
    operation = paths[
        "/api/v1/subjects/{subject_id}/night-episodes"
    ]["get"]
    limit = next(item for item in operation["parameters"] if item["name"] == "limit")
    assert limit["schema"]["maximum"] == 100
    assert set(schema["components"]["securitySchemes"]) == {
        "SleepActorAssertion",
        "SleepServiceCredential",
    }
    public_contract = json.dumps(schema, ensure_ascii=False).lower()
    for forbidden in ("evidence ledger", "tool receipt", "agent id", "task id"):
        assert forbidden not in public_contract
    assert MIGRATION_VERSION == "020_legacy_authority_cutover"
    migration = RADAR_AGENT_POSTGRES_MIGRATIONS["018_sleep_api_v1"]
    for table in (
        "sleep_api_actor_assertion_replays",
        "sleep_api_operation_commands",
        "sleep_api_feedback",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
    assert "DROP TABLE" not in migration.upper()
    assert "DELETE FROM" not in migration.upper()


def test_committed_queries_and_role_views_make_zero_synchronous_reasoning_calls(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "queries.sqlite3", seed=True)
    client = _client(environment)
    before_operations = environment.runtime.repository.count_rows(
        "sleep_domain_operations"
    )
    cases = (
        (
            "/api/v1/subjects/elder-1/lifecycle",
            PublicActorRole.ELDER,
            {"sleep:lifecycle:read"},
            "lifecycle_response.v1",
        ),
        (
            "/api/v1/subjects/elder-1/risk",
            PublicActorRole.ELDER,
            {"sleep:risk:read"},
            "current_risk_response.v1",
        ),
        (
            f"/api/v1/subjects/elder-1/night-episodes/{environment.episode_id}",
            PublicActorRole.FAMILY,
            {"sleep:episode:read"},
            "night_episode_response.v1",
        ),
    )
    for index, (path, role, scopes, schema_version) in enumerate(cases):
        response = client.get(
            path,
            headers=_headers(
                environment,
                method="GET",
                path=path,
                role=role,
                scopes=scopes,
                jti=f"query-{index}",
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["schema_version"] == schema_version
        assert response.headers["x-api-version"] == "v1"
    for role, expected_text in (
        (PublicActorRole.ELDER, "老人版"),
        (PublicActorRole.FAMILY, "家属版"),
        (PublicActorRole.CAREGIVER, "家属版"),
        (PublicActorRole.DOCTOR, "医生版"),
    ):
        path = (
            f"/api/v1/subjects/elder-1/night-episodes/"
            f"{environment.episode_id}/view"
        )
        view_scope = (
            "sleep:view:family"
            if role in {PublicActorRole.FAMILY, PublicActorRole.CAREGIVER}
            else f"sleep:view:{role.value}"
        )
        response = client.get(
            path,
            headers=_headers(
                environment,
                method="GET",
                path=path,
                role=role,
                scopes={view_scope},
                jti=f"view-{role.value}",
            ),
        )
        assert response.status_code == 200, response.text
        assert expected_text in response.json()["content"]
        assert "product_agent_episode_id" not in response.json()
    assert (
        environment.runtime.repository.count_rows("sleep_domain_operations")
        == before_operations
    )


def test_authentication_claim_binding_overreach_and_replay_fail_closed(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "auth.sqlite3", seed=True)
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/lifecycle"
    assert client.get(path).status_code == 401
    invalid_service = _headers(
        environment,
        method="GET",
        path=path,
        role=PublicActorRole.ELDER,
        scopes={"sleep:lifecycle:read"},
        jti="bad-service",
        service_secret="incorrect",
    )
    assert client.get(path, headers=invalid_service).status_code == 401

    replay_headers = _headers(
        environment,
        method="GET",
        path=path,
        role=PublicActorRole.ELDER,
        scopes={"sleep:lifecycle:read"},
        jti="replay-1",
    )
    assert client.get(path, headers=replay_headers).status_code == 200
    replay = client.get(path, headers=replay_headers)
    assert replay.status_code == 401
    assert replay.json()["code"] == "ACTOR_ASSERTION_REPLAYED"

    wrong_path_headers = _headers(
        environment,
        method="GET",
        path="/api/v1/subjects/elder-1/risk",
        role=PublicActorRole.ELDER,
        scopes={"sleep:lifecycle:read"},
        jti="wrong-path",
    )
    assert client.get(path, headers=wrong_path_headers).status_code == 401

    missing_scope = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:risk:read"},
            jti="missing-scope",
        ),
    )
    assert missing_scope.status_code == 403

    other_path = "/api/v1/subjects/elder-2/lifecycle"
    overreach = client.get(
        other_path,
        headers=_headers(
            environment,
            method="GET",
            path=other_path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:lifecycle:read"},
            jti="overreach",
        ),
    )
    assert overreach.status_code == 403
    assert environment.authority.resolve_count >= 2

    command_path = "/api/v1/subjects/elder-1/monitoring/activate"
    body = {
        "schema_version": "activate_monitoring_request.v1",
        "device_binding_id": "binding-1",
    }
    different_body = {
        "schema_version": "activate_monitoring_request.v1",
        "device_binding_id": None,
    }
    body_mismatch = client.post(
        command_path,
        json=body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=command_path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=different_body,
                jti="body-mismatch",
            ),
            "Idempotency-Key": "body-mismatch",
        },
    )
    assert body_mismatch.status_code == 401
    assert body_mismatch.json()["code"] == "INVALID_ACTOR_ASSERTION"


def test_service_and_actor_key_rotation_windows_are_enforced(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "rotation.sqlite3", seed=True)
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/lifecycle"
    for index, (key_id, credential) in enumerate(
        (("old", SERVICE_OLD), ("new", SERVICE_NEW))
    ):
        response = client.get(
            path,
            headers=_headers(
                environment,
                method="GET",
                path=path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:lifecycle:read"},
                jti=f"rotation-{index}",
                key_id=key_id,
                service_secret=credential,
            ),
        )
        assert response.status_code == 200, response.text
    environment.clock.current = START + timedelta(days=11)
    old_service = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:lifecycle:read"},
            jti="old-service-expired",
            service_secret=SERVICE_OLD,
        ),
    )
    assert old_service.status_code == 401
    old_actor = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:lifecycle:read"},
            jti="old-actor-key-expired",
            key_id="old",
            service_secret=SERVICE_NEW,
        ),
    )
    assert old_actor.status_code == 401


@pytest.mark.parametrize(
    ("claim_updates", "expected_status"),
    (
        ({"iss": "unapproved-issuer"}, 401),
        ({"aud": "wrong-audience"}, 401),
        ({"iat": int((START + timedelta(days=3)).timestamp())}, 401),
        (
            {
                "iat": int((START - timedelta(minutes=5)).timestamp()),
                "exp": int((START - timedelta(minutes=4)).timestamp()),
            },
            401,
        ),
        ({"nonce": "too-short"}, 401),
        ({"method": "POST"}, 401),
        ({"actor_id": "unbound-actor"}, 403),
        ({"subject_id": "unbound-subject"}, 403),
    ),
)
def test_actor_assertion_required_claims_are_verified(
    tmp_path: Path,
    claim_updates: dict[str, Any],
    expected_status: int,
) -> None:
    environment = _environment(
        tmp_path / f"claims-{hash(frozenset(claim_updates.items()))}.sqlite3",
        seed=True,
    )
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/lifecycle"
    response = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:lifecycle:read"},
            jti=f"claims-{abs(hash(frozenset(claim_updates.items())))}",
            claim_updates=claim_updates,
        ),
    )
    assert response.status_code == expected_status, response.text


def test_idempotent_commands_return_original_operation_and_conflict_on_body_change(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "idempotency.sqlite3", seed=True)
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/monitoring/activate"
    body = {
        "schema_version": "activate_monitoring_request.v1",
        "device_binding_id": "binding-1",
    }
    first = client.post(
        path,
        json=body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=body,
                jti="idem-first",
            ),
            "Idempotency-Key": "activate-one",
        },
    )
    assert first.status_code == 202, first.text
    second = client.post(
        path,
        json=body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=body,
                jti="idem-second",
            ),
            "Idempotency-Key": "activate-one",
        },
    )
    assert second.status_code == 202
    assert second.json()["operation_id"] == first.json()["operation_id"]
    changed_body = {
        "schema_version": "activate_monitoring_request.v1",
        "device_binding_id": None,
    }
    conflict = client.post(
        path,
        json=changed_body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=changed_body,
                jti="idem-conflict",
            ),
            "Idempotency-Key": "activate-one",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert (
        environment.runtime.repository.count_rows("sleep_domain_operations") == 1
    )
    incompatible_body = {
        "schema_version": "activate_monitoring_request.v2",
        "device_binding_id": "binding-1",
    }
    incompatible = client.post(
        path,
        json=incompatible_body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=incompatible_body,
                jti="incompatible-version",
            ),
            "Idempotency-Key": "incompatible-version",
        },
    )
    assert incompatible.status_code == 422
    assert incompatible.json()["schema_version"] == "error.v1"
    assert incompatible.headers["deprecation"] == "false"


def test_operation_worker_commits_lifecycle_feedback_reanalysis_and_status(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "commands.sqlite3", seed=True)
    client = _client(environment)
    worker = SleepApiOperationWorker(environment.runtime, worker_id="worker-1")
    activate_path = "/api/v1/subjects/elder-1/monitoring/activate"
    activate_body = {
        "schema_version": "activate_monitoring_request.v1",
        "device_binding_id": "binding-1",
    }
    accepted = client.post(
        activate_path,
        json=activate_body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=activate_path,
                role=PublicActorRole.ELDER,
                scopes={"sleep:monitoring:write"},
                body=activate_body,
                jti="worker-activate",
            ),
            "Idempotency-Key": "worker-activate",
        },
    )
    assert accepted.status_code == 202
    assert worker.run_once()
    operation_id = accepted.json()["operation_id"]
    status_path = f"/api/v1/operations/{operation_id}"
    status = client.get(
        status_path,
        headers=_headers(
            environment,
            method="GET",
            path=status_path,
            role=PublicActorRole.ELDER,
            scopes={"sleep:operation:read"},
            jti="worker-status",
        ),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["attempt"] == 1
    assert status.json()["result_resource_id"]
    assert "lease_owner" not in status.json()

    feedback_path = "/api/v1/subjects/elder-1/feedback/family"
    feedback_body = {
        "schema_version": "feedback_request.v1",
        "night_episode_id": environment.episode_id,
        "event_at": environment.clock().isoformat(),
        "source_text": "昨晚老人说醒来后仍然疲倦。",
    }
    feedback = client.post(
        feedback_path,
        json=feedback_body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=feedback_path,
                role=PublicActorRole.FAMILY,
                scopes={"sleep:feedback:family"},
                body=feedback_body,
                jti="family-feedback",
            ),
            "Idempotency-Key": "family-feedback",
        },
    )
    assert feedback.status_code == 202, feedback.text
    assert worker.run_once()
    assert environment.runtime.api_persistence.count_feedback(NAMESPACE) == 1

    reanalysis_path = (
        f"/api/v1/subjects/elder-1/night-episodes/"
        f"{environment.episode_id}/reanalysis"
    )
    reanalysis_body = {
        "schema_version": "reanalysis_request.v1",
        "reason": "incorporate authorized feedback",
    }
    reanalysis = client.post(
        reanalysis_path,
        json=reanalysis_body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=reanalysis_path,
                role=PublicActorRole.DOCTOR,
                scopes={"sleep:reanalysis:write"},
                body=reanalysis_body,
                jti="doctor-reanalysis",
            ),
            "Idempotency-Key": "doctor-reanalysis",
        },
    )
    assert reanalysis.status_code == 202, reanalysis.text
    assert worker.run_once()
    episode = environment.runtime.repository.get_night_episode(
        NAMESPACE,
        night_episode_id=environment.episode_id or "",
    )
    assert episode is not None
    assert len(episode.night_episode_revision_ids) == 3
    row = environment.store.connection.execute(
        """
        SELECT actor_role, provenance_category, authorization_epoch
        FROM sleep_api_feedback
        """
    ).fetchone()
    assert row == ("family", "family_report", 1)


def test_authorization_epoch_cache_isolates_role_scope_and_revocation(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "cache.sqlite3", seed=True)
    client = _client(environment)
    path = (
        f"/api/v1/subjects/elder-1/night-episodes/"
        f"{environment.episode_id}/view"
    )
    family = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:view:family"},
            jti="cache-family",
        ),
    )
    doctor = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.DOCTOR,
            scopes={"sleep:view:doctor"},
            jti="cache-doctor",
        ),
    )
    assert "家属版" in family.json()["content"]
    assert "医生版" in doctor.json()["content"]
    old_binding = next(
        item for item in environment.bindings if item.role == PublicActorRole.FAMILY
    )
    environment.authority.put(
        AuthoritativeRoleBinding(
            **{
                **old_binding.__dict__,
                "authorization_epoch": 2,
            }
        )
    )
    refreshed = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:view:family"},
            jti="cache-family-epoch-2",
        ),
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["authorization_epoch"] == 2
    environment.authority.put(
        AuthoritativeRoleBinding(
            **{
                **old_binding.__dict__,
                "authorization_epoch": 3,
                "active": False,
            }
        )
    )
    revoked = client.get(
        path,
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:view:family"},
            jti="cache-family-revoked",
        ),
    )
    assert revoked.status_code == 403


def test_pagination_cursor_is_bound_to_actor_role_scope_and_authorization_epoch(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "pagination.sqlite3", seed=True)
    original = environment.runtime.repository.get_night_episode(
        NAMESPACE,
        night_episode_id=environment.episode_id or "",
    )
    assert original is not None
    older = original.model_copy(
        update={
            "night_episode_id": "episode-older",
            "local_sleep_date": original.local_sleep_date - timedelta(days=1),
            "night_key": "elder-1:older:boundary.v1",
            "collection_start_at": original.collection_start_at
            - timedelta(days=1),
            "collection_end_at": original.collection_end_at - timedelta(days=1),
            "allowed_lateness_watermark_at": (
                original.allowed_lateness_watermark_at - timedelta(days=1)
            ),
            "report_deadline_at": original.report_deadline_at - timedelta(days=1),
            "night_episode_revision_ids": (),
            "transition_receipt_ids": (),
            "current_night_episode_revision_id": None,
            "created_at": original.created_at - timedelta(days=1),
            "updated_at": original.updated_at - timedelta(days=1),
        }
    )
    environment.runtime.repository.create_night_episode(NAMESPACE, older)
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/night-episodes"
    first = client.get(
        path,
        params={"limit": 1},
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:episode:read"},
            jti="page-first",
        ),
    )
    assert first.status_code == 200, first.text
    cursor = first.json()["page"]["next_cursor"]
    assert cursor
    second = client.get(
        path,
        params={"limit": 1, "cursor": cursor},
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:episode:read"},
            jti="page-second",
        ),
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["night_episode_id"] == "episode-older"
    cross_role = client.get(
        path,
        params={"limit": 1, "cursor": cursor},
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.DOCTOR,
            scopes={"sleep:episode:read"},
            jti="page-cross-role",
        ),
    )
    assert cross_role.status_code == 403
    family_binding = next(
        item for item in environment.bindings if item.role == PublicActorRole.FAMILY
    )
    environment.authority.put(
        AuthoritativeRoleBinding(
            **{**family_binding.__dict__, "authorization_epoch": 2}
        )
    )
    stale_epoch = client.get(
        path,
        params={"limit": 1, "cursor": cursor},
        headers=_headers(
            environment,
            method="GET",
            path=path,
            role=PublicActorRole.FAMILY,
            scopes={"sleep:episode:read"},
            jti="page-stale-epoch",
        ),
    )
    assert stale_epoch.status_code == 403


def test_worker_rechecks_authority_and_fails_queued_command_after_revocation(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "queued-revocation.sqlite3", seed=True)
    client = _client(environment)
    path = "/api/v1/subjects/elder-1/feedback/family"
    body = {
        "schema_version": "feedback_request.v1",
        "night_episode_id": environment.episode_id,
        "event_at": environment.clock().isoformat(),
        "source_text": "queued feedback",
    }
    accepted = client.post(
        path,
        json=body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.FAMILY,
                scopes={"sleep:feedback:family"},
                body=body,
                jti="queued-revocation",
            ),
            "Idempotency-Key": "queued-revocation",
        },
    )
    assert accepted.status_code == 202
    binding = next(
        item for item in environment.bindings if item.role == PublicActorRole.FAMILY
    )
    environment.authority.put(
        AuthoritativeRoleBinding(
            **{
                **binding.__dict__,
                "authorization_epoch": 2,
                "active": False,
            }
        )
    )
    worker = SleepApiOperationWorker(environment.runtime)
    assert worker.run_once()
    operation = environment.runtime.repository.get_operation(
        NAMESPACE,
        operation_id=accepted.json()["operation_id"],
    )
    assert operation is not None
    assert operation.status.value == "failed"
    assert operation.error_code == "AUTHORIZATION_DENIED"
    assert environment.runtime.api_persistence.count_feedback(NAMESPACE) == 0


def test_worker_lease_retry_does_not_duplicate_committed_reanalysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path / "lease-retry.sqlite3", seed=True)
    client = _client(environment)
    path = (
        f"/api/v1/subjects/elder-1/night-episodes/"
        f"{environment.episode_id}/reanalysis"
    )
    body = {
        "schema_version": "reanalysis_request.v1",
        "reason": "lease retry proof",
    }
    accepted = client.post(
        path,
        json=body,
        headers={
            **_headers(
                environment,
                method="POST",
                path=path,
                role=PublicActorRole.DOCTOR,
                scopes={"sleep:reanalysis:write"},
                body=body,
                jti="lease-retry",
            ),
            "Idempotency-Key": "lease-retry",
        },
    )
    assert accepted.status_code == 202
    repository = environment.runtime.repository
    original_compare = repository.compare_and_set_operation
    monkeypatch.setattr(repository, "compare_and_set_operation", lambda *a, **k: False)
    worker = SleepApiOperationWorker(
        environment.runtime,
        worker_id="crashing-worker",
        lease_duration=timedelta(seconds=1),
    )
    with pytest.raises(Exception, match="lease"):
        worker.run_once()
    episode_after_commit = repository.get_night_episode(
        NAMESPACE,
        night_episode_id=environment.episode_id or "",
    )
    assert episode_after_commit is not None
    assert len(episode_after_commit.night_episode_revision_ids) == 2
    monkeypatch.setattr(repository, "compare_and_set_operation", original_compare)
    environment.clock.advance(timedelta(seconds=2))
    retry_worker = SleepApiOperationWorker(
        environment.runtime,
        worker_id="retry-worker",
        lease_duration=timedelta(seconds=1),
    )
    assert retry_worker.run_once()
    episode_after_retry = repository.get_night_episode(
        NAMESPACE,
        night_episode_id=environment.episode_id or "",
    )
    assert episode_after_retry is not None
    assert len(episode_after_retry.night_episode_revision_ids) == 2
    operation = repository.get_operation(
        NAMESPACE,
        operation_id=accepted.json()["operation_id"],
    )
    assert operation is not None
    assert operation.status.value == "succeeded"
    assert operation.attempt_count == 2


def test_replay_and_pending_operation_survive_process_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.sqlite3"
    first = _environment(database_path, seed=True)
    client = _client(first)
    query_path = "/api/v1/subjects/elder-1/lifecycle"
    replay_headers = _headers(
        first,
        method="GET",
        path=query_path,
        role=PublicActorRole.ELDER,
        scopes={"sleep:lifecycle:read"},
        jti="persistent-replay",
    )
    assert client.get(query_path, headers=replay_headers).status_code == 200
    command_path = (
        f"/api/v1/subjects/elder-1/night-episodes/"
        f"{first.episode_id}/reanalysis"
    )
    command_body = {
        "schema_version": "reanalysis_request.v1",
        "reason": "restart recovery",
    }
    accepted = client.post(
        command_path,
        json=command_body,
        headers={
            **_headers(
                first,
                method="POST",
                path=command_path,
                role=PublicActorRole.DOCTOR,
                scopes={"sleep:reanalysis:write"},
                body=command_body,
                jti="restart-command",
            ),
            "Idempotency-Key": "restart-command",
        },
    )
    assert accepted.status_code == 202
    first.store.connection.close()

    restarted = _environment(
        database_path,
        seed=False,
        clock=first.clock,
        private_keys=first.private_keys,
    )
    restarted_client = _client(restarted)
    replayed = restarted_client.get(query_path, headers=replay_headers)
    assert replayed.status_code == 401
    assert replayed.json()["code"] == "ACTOR_ASSERTION_REPLAYED"
    worker = SleepApiOperationWorker(restarted.runtime, worker_id="restart-worker")
    assert worker.run_once()
    operation_id = accepted.json()["operation_id"]
    operation = restarted.runtime.repository.get_operation(
        NAMESPACE,
        operation_id=operation_id,
    )
    assert operation is not None
    assert operation.status.value == "succeeded"
    assert operation.attempt_count == 1
    assert operation.result_resource_id
    status_path = f"/api/v1/operations/{operation_id}"
    status_response = restarted_client.get(
        status_path,
        headers=_headers(
            restarted,
            method="GET",
            path=status_path,
            role=PublicActorRole.DOCTOR,
            scopes={"sleep:operation:read"},
            jti="restart-operation-status",
        ),
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "succeeded"


def test_production_configuration_fails_closed_without_authority_or_durable_store(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        build_sleep_api_runtime_from_env(
            environment={
                "SLEEPAGENT_SLEEP_API_MODE": "production",
                "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH": str(
                    tmp_path / "not-production.sqlite3"
                ),
            }
        )
