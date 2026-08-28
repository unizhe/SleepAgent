from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError

import sleepagent.api.postgres as postgres_module
from sleepagent.domain.habit import HabitProfileState
from sleepagent.api.product_contracts import (
    ProductNarrativeState,
    ProductReportNarrative,
    ProductReportProjection,
    ProductReportQuality,
    ProductReportRunRequest,
    ProductReportState,
    ProductRole,
    ProductSleepReportResponse,
)
from sleepagent.api.postgres import (
    PostgresAuthorityStore,
    PostgresProductBackend,
    _ReportReadRow,
    _current_report_context_sha256,
    _default_elder_narrative_manifest_sha256,
    _default_shared_runtime_manifest_sha256,
    _report_trace,
    _report_response,
    _resolved_report_model_mode,
)
from sleepagent.api.product import ProductApiError, ProductRequestContext
from sleepagent.runtime.contracts import AgentId, SourceScopeKind, stable_hash
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.factory import (
    build_deterministic_product_runtime_bundle,
    build_product_runtime_bundle_from_env,
)
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
    MemoryPurpose,
    MemoryQueryIntent,
    ProvenanceType,
    SensitivityClass,
    resolve_memory_query,
    select_memory_slice,
)
from sleepagent.runtime.reports import (
    RoleProjection,
    SharedNightAnalysis,
    build_role_projection_runtime_manifest,
    build_shared_role_projections,
    role_projection_identity_sha256,
)
from sleepagent.runtime.results import PinnedPersonalizationContext
from sleepagent.workers.product import (
    _consumed_context_sha256,
    _elder_narrative_runtime_manifest,
    _shared_runtime_manifest,
)
from sleepagent.api.public_auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    ServicePrincipal,
    SleepApiSecurityError,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


class CountingReplayStore:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: set[tuple[str, str, str]] = set()

    def consume(
        self,
        *,
        issuer: str,
        assertion_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        del expires_at, now
        self.calls += 1
        key = (issuer, assertion_id, nonce)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executions: list[tuple[str, Any]] = []
        self.current_statement = ""

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.current_statement = statement
        self.executions.append((statement, parameters))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        if (
            "backend_habit_profile_revisions_v2" in self.current_statement
            or "backend_governed_memory_revisions_v2" in self.current_statement
        ):
            return []
        return list(self.rows)

    def close(self) -> None:
        return None


class FakeUow:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cursor = FakeCursor(rows)
        self.connection = SimpleNamespace(cursor=lambda: self.cursor)
        self.commit_calls = 0
        self.exited = False

    def __enter__(self) -> "FakeUow":
        return self

    def __exit__(self, *_args: object) -> bool:
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_calls += 1
        raise AssertionError("read-only report path attempted to commit")


class FakeUowFactory:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.units: list[FakeUow] = []

    def begin(self, _scope: object) -> FakeUow:
        unit = FakeUow(self.rows)
        self.units.append(unit)
        return unit


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _assertion(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    now: datetime,
    method: str = "GET",
    path: str = "/product/sleep/reports/2026-08-26",
    body: bytes = b"",
) -> str:
    header = {"alg": "EdDSA", "kid": "actor-key-1", "typ": "JWT"}
    payload: dict[str, Any] = {
        "iss": "trusted-issuer",
        "aud": "sleep-api",
        "jti": "assertion-1",
        "actor_id": "actor-1",
        "subject_id": "subject-1",
        "role": "elder",
        "scope": ["product:sleep:today:read"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=2)).timestamp()),
        "nonce": "nonce-at-least-sixteen-characters",
        "method": method,
        "path": path,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_policy_epoch": 1,
    }
    encoded_header = _b64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    encoded_payload = _b64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    return (
        f"{encoded_header}.{encoded_payload}."
        + _b64url(private_key.sign(signing_input))
    )


def _verifier() -> tuple[
    ActorAssertionVerifier,
    CountingReplayStore,
    ed25519.Ed25519PrivateKey,
    ServicePrincipal,
    datetime,
]:
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    replay_store = CountingReplayStore()
    verifier = ActorAssertionVerifier(
        (
            ActorVerificationKey(
                issuer="trusted-issuer",
                key_id="actor-key-1",
                algorithm="EdDSA",
                public_key_pem=public_pem,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=1),
            ),
        ),
        audience="sleep-api",
        replay_store=replay_store,
    )
    principal = ServicePrincipal(
        principal_id="trusted-bff",
        credential_id="service-key-1",
        allowed_actor_issuers=frozenset({"trusted-issuer"}),
    )
    return verifier, replay_store, private_key, principal, now


def _context() -> ProductRequestContext:
    return ProductRequestContext(
        service_principal_id="trusted-bff",
        actor_id="actor-1",
        binding_id="binding-1",
        subject_id="subject-1",
        role=ProductRole.ELDER,
        effective_scopes=frozenset({"product:sleep:today:read"}),
        namespace_id="replay:test",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-1",
        arm_id="arm-1",
        purpose="sleep_care",
        authorization_epoch=1,
        privacy_epoch=2,
        retrieval_epoch=3,
        policy_sha256="a" * 64,
    )


def _report_row(**updates: Any) -> _ReportReadRow:
    row = _ReportReadRow(
        wake_date=date(2026, 8, 26),
        quality_json={"data_sufficiency": "sufficient"},
        risk_json={"health_escalation_allowed": False},
        request_status=None,
        request_json=None,
        shared_status=None,
        analysis_json=None,
        view_status=None,
        view_json=None,
        view_fact_snapshot_sha256=None,
        view_projection_identity_sha256=None,
        narrative_status=None,
        narrative_operation_id=None,
        narrative_operation_json=None,
        narrative_json=None,
        has_stale_artifact=False,
        source_revision_valid=True,
        source_date_match_count=1,
        shared_failed_attempts=(),
        narrative_failed_attempts=(),
        shared_orphaned_journal_usage=(),
        narrative_orphaned_journal_usage=(),
        current_context_sha256="c" * 64,
        current_runtime_manifest_sha256="r" * 64,
        current_projection_manifest_sha256=stable_hash(
            build_role_projection_runtime_manifest()
        ),
        current_narrative_manifest_sha256="n" * 64,
    )
    return replace(row, **updates)


def _report_db_row(
    *,
    quality_json: dict[str, Any] | None = None,
    risk_json: dict[str, Any] | None = None,
    source_revision_valid: bool = True,
    source_date_match_count: int = 1,
) -> tuple[Any, ...]:
    return (
        date(2026, 8, 26),
        quality_json,
        risk_json,
        None,  # request status
        None,  # request JSON
        None,  # shared status
        None,  # analysis JSON
        None,  # role-view status
        None,  # role-view JSON
        None,  # role-view fact hash
        None,  # role-view projection identity
        None,  # narrative status
        None,  # narrative operation JSON
        None,  # narrative attempt JSON
        False,  # stale artifact exists
        source_revision_valid,
        source_date_match_count,
        None,  # narrative operation ID
        None,  # shared uncommitted attempts
        None,  # narrative uncommitted attempts
        None,  # shared orphaned journal usage
        None,  # narrative orphaned journal usage
    )


@lru_cache(maxsize=1)
def _valid_shared_artifacts() -> tuple[
    SharedNightAnalysis,
    RoleProjection,
    dict[str, str],
    dict[str, Any],
    str,
]:
    from tests.unit.test_product_agent_runner import _shared_analysis_request

    bundle = build_deterministic_product_runtime_bundle(
        model=DeterministicReplayStructuredAgentModel()
    )
    shared = bundle.runner.analyze_shared(_shared_analysis_request())
    projections = build_shared_role_projections(shared)
    elder = projections[0]
    manifest = build_role_projection_runtime_manifest()
    manifest_sha256 = stable_hash(manifest)
    identities = {
        projection.role.value: role_projection_identity_sha256(
            desired_analysis_sha256=(
                shared.source.desired_analysis_sha256
            ),
            role=projection.role,
            projection_sha256=projection.projection_sha256,
            projection_manifest_sha256=manifest_sha256,
        )
        for projection in projections
    }
    return shared, elder, identities, manifest, manifest_sha256


def _compatible_ready_row() -> _ReportReadRow:
    shared, elder, identities, projection_manifest, projection_manifest_sha256 = (
        _valid_shared_artifacts()
    )
    desired = shared.source.desired_analysis_sha256
    return _report_row(
        wake_date=shared.source.wake_date,
        current_context_sha256=shared.source.consumed_context_sha256,
        current_runtime_manifest_sha256=shared.source.runtime_manifest_sha256,
        current_projection_manifest_sha256=projection_manifest_sha256,
        request_status="succeeded",
        request_json={
            "report_result": {
                "schema_version": "product_report_request_result.v1",
                "state": "pending",
                "gate": "analyzable",
                "shared_operation_created": True,
                "shared_operation_id": "internal-shared-operation",
                "desired_analysis_sha256": desired,
            }
        },
        shared_status="succeeded",
        analysis_json={
            "schema_version": "shared_night_analysis.v1",
            "desired_analysis_sha256": desired,
            "consumed_context_sha256": (
                shared.source.consumed_context_sha256
            ),
            "runtime_manifest_sha256": (
                shared.source.runtime_manifest_sha256
            ),
            "projection_manifest": projection_manifest,
            "projection_manifest_sha256": projection_manifest_sha256,
            "projection_identities": identities,
            "provider_usage": {
                "call_count": 2,
                "input_tokens": 100,
                "output_tokens": 40,
                "request_ids_present": False,
            },
            "shared_analysis": shared.model_dump(mode="json"),
        },
        view_status="ready",
        view_json=elder.model_dump(mode="json"),
        view_fact_snapshot_sha256=shared.fact_snapshot_hash,
        view_projection_identity_sha256=identities["elder"],
    )


def test_report_request_accepts_only_canonical_date_and_public_fields() -> None:
    request = ProductReportRunRequest.model_validate(
        {
            "schema_version": "product_sleep_report_run.v1",
            "wake_date": "2026-08-26",
        }
    )

    assert request.wake_date == date(2026, 8, 26)
    with pytest.raises(ValidationError):
        ProductReportRunRequest.model_validate(
            {
                "wake_date": "2026-08-26",
                "subject_id": "caller-selected-subject",
            }
        )
    with pytest.raises(ValidationError):
        ProductReportRunRequest.model_validate({"wake_date": "2026-8-26"})
    with pytest.raises(ValidationError):
        ProductReportRunRequest.model_validate(
            {"wake_date": "2026-08-26T00:00:00Z"}
        )
    with pytest.raises(ValidationError):
        ProductReportRunRequest.model_validate({"wake_date": 0})


def test_api_live_manifest_hashes_match_the_worker_runtime() -> None:
    context = _context()
    source = SimpleNamespace(
        subject_id=context.subject_id,
        facts=SimpleNamespace(
            data_mode=SimpleNamespace(value=context.data_mode),
        ),
    )
    bundle = build_product_runtime_bundle_from_env()

    assert _default_shared_runtime_manifest_sha256(
        context,
        model_mode="live",
    ) == stable_hash(
        _shared_runtime_manifest(bundle, source=source)  # type: ignore[arg-type]
    )
    assert _default_elder_narrative_manifest_sha256(
        context,
        model_mode="live",
    ) == stable_hash(
        _elder_narrative_runtime_manifest(  # type: ignore[arg-type]
            bundle,
            source=source,
        )
    )


def test_api_deterministic_manifest_hashes_match_the_worker_runtime() -> None:
    context = _context()
    source = SimpleNamespace(
        subject_id=context.subject_id,
        facts=SimpleNamespace(
            data_mode=SimpleNamespace(value=context.data_mode),
        ),
    )
    bundle = build_deterministic_product_runtime_bundle(
        model=DeterministicReplayStructuredAgentModel(
            deployment_mode="test",
            data_mode="replay",
        )
    )

    assert _default_shared_runtime_manifest_sha256(
        context,
        model_mode="deterministic",
        deployment_mode="test",
    ) == stable_hash(
        _shared_runtime_manifest(bundle, source=source)  # type: ignore[arg-type]
    )
    assert _default_elder_narrative_manifest_sha256(
        context,
        model_mode="deterministic",
        deployment_mode="test",
    ) == stable_hash(
        _elder_narrative_runtime_manifest(  # type: ignore[arg-type]
            bundle,
            source=source,
        )
    )


def test_nonempty_memory_context_hash_matches_worker_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    as_of = datetime(2026, 8, 27, 8, tzinfo=UTC)
    memory_state = GovernedMemoryState(
        subject_id=context.subject_id,
        version=1,
        revisions=(
            GovernedMemoryItemV2(
                memory_id="memory:care-preference",
                subject_id=context.subject_id,
                memory_type="communication_preference",
                concept_id="sleep.preference.care_delivery",
                value_schema_id="enum.v1",
                typed_value="morning_voice",
                provenance_type=ProvenanceType.ELDER_CONFIRMED,
                source_ref="user_report:care-preference",
                source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
                version=1,
                recorded_at=as_of,
                valid_from=as_of,
                sensitivity_class=SensitivityClass.PERSONAL,
                allowed_roles=(AgentId.CARE_STRATEGY,),
                allowed_purposes=(MemoryPurpose.CARE_PREFERENCE_CONTEXT,),
                confirmation_ref="confirmation:memory:1",
                retention_policy_version="sleepagent-retention.v1",
            ),
        ),
    )
    receipts = []
    for requesting_agent, purpose, concept_ids in (
        (
            AgentId.EVIDENCE_REASONING,
            MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
            postgres_module._REPORT_EVIDENCE_MEMORY_CONCEPT_IDS,
        ),
        (
            AgentId.CARE_STRATEGY,
            MemoryPurpose.CARE_PREFERENCE_CONTEXT,
            postgres_module._REPORT_CARE_MEMORY_CONCEPT_IDS,
        ),
    ):
        query = resolve_memory_query(
            MemoryQueryIntent(
                purpose=purpose,
                concept_ids=concept_ids,
                source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
                max_items=4,
                token_budget=800,
            ),
            invocation_id=f"worker-parity:{requesting_agent.value}",
            actor_id=f"workload:{context.service_principal_id}",
            actor_role="system",
            subject_id=context.subject_id,
            requesting_agent=requesting_agent,
            authorization_scope=("memory:read",),
            as_of=as_of,
            privacy_epoch=context.privacy_epoch,
            authorization_epoch=context.authorization_epoch,
        )
        receipts.append(select_memory_slice(query, memory_state, now=as_of))
    personalization = PinnedPersonalizationContext(
        subject_id=context.subject_id,
        memory_state_version=memory_state.version,
        memory_read_receipts=tuple(receipts),
    )
    assert sum(len(receipt.items) for receipt in receipts) == 1
    assert isinstance(receipts[1].items[0].value_schema_id, str)

    monkeypatch.setattr(
        postgres_module,
        "_load_habit_profile",
        lambda _cursor, _context: HabitProfileState(
            subject_id=context.subject_id
        ),
    )
    monkeypatch.setattr(
        postgres_module,
        "_load_memory_state",
        lambda _cursor, _context: memory_state,
    )

    api_hash = _current_report_context_sha256(
        object(),
        context,
        as_of=as_of,
    )
    worker_hash = _consumed_context_sha256(
        personalization,
        authorization_epoch=context.authorization_epoch,
        privacy_epoch=context.privacy_epoch,
        retrieval_policy_epoch=context.retrieval_epoch,
    )

    assert api_hash == worker_hash


def test_report_model_mode_override_is_bounded_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE", "live")
    backend = PostgresProductBackend(
        FakeUowFactory([]),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    assert backend.report_model_mode == "live"
    assert _resolved_report_model_mode(_context(), configured="live") == "live"
    assert (
        _resolved_report_model_mode(_context(), configured=None)
        == "deterministic"
    )

    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE", "disabled")
    with pytest.raises(ValueError, match="must be live or deterministic"):
        PostgresProductBackend(
            FakeUowFactory([]),  # type: ignore[arg-type]
            cursor_key=b"c" * 32,
        )


def test_report_contract_requires_partial_caveat_and_role_binding() -> None:
    with pytest.raises(ValidationError, match="requires a caveat"):
        ProductSleepReportResponse(
            wake_date=date(2026, 8, 26),
            state=ProductReportState.NOT_RUN,
            audience=ProductRole.ELDER,
            quality=ProductReportQuality.PARTIAL,
        )

    with pytest.raises(ValidationError, match="audience must match"):
        ProductSleepReportResponse(
            wake_date=date(2026, 8, 26),
            state=ProductReportState.READY,
            audience=ProductRole.FAMILY,
            quality=ProductReportQuality.GOOD,
            projection=ProductReportProjection(
                audience=ProductRole.DOCTOR,
                summary_text="Bound report",
                context_notice="Authorized context only.",
            ),
        )

    with pytest.raises(ValidationError, match="cannot cross"):
        ProductSleepReportResponse(
            wake_date=date(2026, 8, 26),
            state=ProductReportState.READY,
            audience=ProductRole.FAMILY,
            quality=ProductReportQuality.GOOD,
            projection=ProductReportProjection(
                audience=ProductRole.FAMILY,
                summary_text="Bound report",
                context_notice="Authorized context only.",
            ),
            narrative=ProductReportNarrative(
                state=ProductNarrativeState.FALLBACK,
            ),
        )


def test_idempotent_read_verifier_reuses_signature_without_replay_write() -> None:
    verifier, replay_store, private_key, principal, now = _verifier()
    compact_jws = _assertion(private_key, now=now)

    first = verifier.verify_idempotent_read(
        compact_jws=compact_jws,
        service_principal=principal,
        method="GET",
        path="/product/sleep/reports/2026-08-26",
        body=b"",
        now=now,
    )
    second = verifier.verify_idempotent_read(
        compact_jws=compact_jws,
        service_principal=principal,
        method="GET",
        path="/product/sleep/reports/2026-08-26",
        body=b"",
        now=now,
    )

    assert first == second
    assert replay_store.calls == 0


def test_command_verifier_still_consumes_and_rejects_replay() -> None:
    verifier, replay_store, private_key, principal, now = _verifier()
    compact_jws = _assertion(private_key, now=now)
    values = {
        "compact_jws": compact_jws,
        "service_principal": principal,
        "method": "GET",
        "path": "/product/sleep/reports/2026-08-26",
        "body": b"",
        "now": now,
    }

    verifier.verify(**values)
    with pytest.raises(SleepApiSecurityError, match="already been used"):
        verifier.verify(**values)

    assert replay_store.calls == 2


def test_stateless_verifier_is_fail_closed_outside_empty_get() -> None:
    verifier, replay_store, private_key, principal, now = _verifier()
    post_assertion = _assertion(private_key, now=now, method="POST")

    with pytest.raises(SleepApiSecurityError, match="empty-body GETs"):
        verifier.verify_idempotent_read(
            compact_jws=post_assertion,
            service_principal=principal,
            method="POST",
            path="/product/sleep/reports/2026-08-26",
            body=b"",
            now=now,
        )

    assert replay_store.calls == 0

    wrong_path = _assertion(
        private_key,
        now=now,
        path="/product/sleep/today",
    )
    with pytest.raises(SleepApiSecurityError, match="Product report"):
        verifier.verify_idempotent_read(
            compact_jws=wrong_path,
            service_principal=principal,
            method="GET",
            path="/product/sleep/today",
            body=b"",
            now=now,
        )
    assert replay_store.calls == 0


def test_report_show_and_list_transactions_are_select_only_and_rollback() -> None:
    report_row = _report_db_row(
        quality_json={"data_sufficiency": "sufficient"},
        risk_json={"health_escalation_allowed": False},
    )
    uow_factory = FakeUowFactory([report_row])
    backend = PostgresProductBackend(
        uow_factory,  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    shown = backend.get_report(
        _context(),
        wake_date=date(2026, 8, 26),
        trace=False,
    )
    listing = backend.list_reports(
        _context(),
        limit=20,
        cursor=None,
        trace=False,
    )

    assert shown is not None and shown.state == ProductReportState.NOT_RUN
    assert listing.items[0].state == ProductReportState.NOT_RUN
    assert len(uow_factory.units) == 2
    for unit in uow_factory.units:
        assert unit.exited is True
        assert unit.commit_calls == 0
        assert len(unit.cursor.executions) == 3
        for statement, _parameters in unit.cursor.executions:
            lowered = statement.lower()
            assert lowered.lstrip().startswith("select")
            assert "insert into" not in lowered
            assert "update " not in lowered
            assert "delete from" not in lowered
        report_select = unit.cursor.executions[0][0]
        assert "{report_result,shared_operation_id}" in report_select
        assert "attempt.operation_id = shared.operation_id" in report_select
        assert "{result,product_attempt_id}" in report_select
        assert "{result,analysis_revision_id}" in report_select
        assert "{report_result,desired_analysis_sha256}" in report_select
        assert "product_provider_failed_attempt.v1" in report_select
        assert "product.shared_analysis.v1" in report_select
        assert "product.elder_narrative.v1" in report_select
        assert "attempt.query_visible = FALSE" in report_select
        assert "jsonb_agg" in report_select
        assert "product_agent_prepared_attempt.v3" in report_select
        assert "product_elder_narrative_prepared_attempt.v1" in report_select
        assert "backend_invocation_journal" in report_select
        assert "staged.product_attempt_id" in report_select
        assert "{response,artifact,provider_usage}" in report_select


def test_report_show_rejects_broken_current_revision_invariant() -> None:
    report_row = _report_db_row(
        source_revision_valid=False,
    )
    backend = PostgresProductBackend(
        FakeUowFactory([report_row]),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    with pytest.raises(ProductApiError) as caught:
        backend.get_report(
            _context(),
            wake_date=date(2026, 8, 26),
            trace=False,
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "report_source_conflict"


def test_report_list_rejects_ambiguous_wake_date() -> None:
    report_row = _report_db_row(
        source_date_match_count=2,
    )
    backend = PostgresProductBackend(
        FakeUowFactory([report_row]),  # type: ignore[arg-type]
        cursor_key=b"c" * 32,
    )

    with pytest.raises(ProductApiError) as caught:
        backend.list_reports(
            _context(),
            limit=20,
            cursor=None,
            trace=False,
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "report_source_conflict"


def test_current_authority_resolution_is_select_only_and_rollback() -> None:
    authority_row = (
        "replay:test",
        "replay",
        1,
        "run-1",
        "arm-1",
        "binding-1",
        "elder",
        ["product:sleep:today:read"],
        1,
        2,
        3,
    )
    uow_factory = FakeUowFactory([authority_row])
    settings = SimpleNamespace(
        data_mode=SimpleNamespace(value="replay"),
        service_principal_id="trusted-bff",
    )
    authority = PostgresAuthorityStore(
        settings,  # type: ignore[arg-type]
        uow_factory,  # type: ignore[arg-type]
    )

    resolved = authority.resolve(
        actor_id="actor-1",
        subject_id="subject-1",
        role=ProductRole.ELDER,
        purpose="sleep_care",
    )

    assert resolved.binding_id == "binding-1"
    unit = uow_factory.units[0]
    assert unit.exited is True
    assert unit.commit_calls == 0
    assert unit.cursor.executions[0][0].lstrip().lower().startswith("select")


def test_pending_report_does_not_misclassify_its_request_as_stale() -> None:
    row = _report_row(
        request_status="succeeded",
        request_json={
            "report_result": {
                "schema_version": "product_report_request_result.v1",
                "state": "pending",
                "gate": "analyzable",
                "shared_operation_created": True,
                "shared_operation_id": "internal-shared-operation",
            }
        },
        shared_status="pending",
        has_stale_artifact=True,
    )

    report = _report_response(_context(), row, include_trace=True)

    assert report.state == ProductReportState.PENDING
    assert report.trace is not None
    assert report.trace.shared_analysis == "created"


def test_gate_states_require_typed_successful_worker_closure() -> None:
    uncompleted = _report_row(
        request_status="running",
        request_json={},
        risk_json={"health_escalation_allowed": True},
    )
    completed = replace(
        uncompleted,
        request_status="succeeded",
        request_json={
            "report_result": {
                "schema_version": "product_report_request_result.v1",
                "state": "urgent_handled",
                "gate": "urgent",
            }
        },
    )

    assert (
        _report_response(_context(), uncompleted, include_trace=False).state
        == ProductReportState.PENDING
    )
    assert (
        _report_response(_context(), completed, include_trace=False).state
        == ProductReportState.URGENT_HANDLED
    )


def test_current_context_or_runtime_drift_marks_committed_report_stale() -> None:
    current = _compatible_ready_row()

    assert (
        _report_response(_context(), current, include_trace=False).state
        == ProductReportState.READY
    )
    assert (
        _report_response(
            _context(),
            replace(current, current_context_sha256="x" * 64),
            include_trace=False,
        ).state
        == ProductReportState.STALE
    )
    assert (
        _report_response(
            _context(),
            replace(current, current_runtime_manifest_sha256="y" * 64),
            include_trace=False,
        ).state
        == ProductReportState.STALE
    )


@pytest.mark.parametrize("missing_desired", [None, 42, "not-a-sha256"])
def test_ready_report_requires_exact_request_desired_analysis_hash(
    missing_desired: object,
) -> None:
    ready = _compatible_ready_row()
    assert ready.request_json is not None
    request_json = dict(ready.request_json)
    result = dict(request_json["report_result"])
    if missing_desired is None:
        result.pop("desired_analysis_sha256")
    else:
        result["desired_analysis_sha256"] = missing_desired
    request_json["report_result"] = result

    report = _report_response(
        _context(),
        replace(ready, request_json=request_json),
        include_trace=False,
    )

    assert report.state == ProductReportState.STALE
    assert report.projection is None


def test_corrupt_shared_analysis_or_projection_never_publishes_text() -> None:
    ready = _compatible_ready_row()
    assert ready.analysis_json is not None
    assert ready.view_json is not None
    corrupt_shared_json = dict(ready.analysis_json)
    corrupt_shared = dict(corrupt_shared_json["shared_analysis"])
    corrupt_shared["summary_lines"] = ["Arbitrary injected summary."]
    corrupt_shared_json["shared_analysis"] = corrupt_shared
    corrupt_projection = {
        **ready.view_json,
        "text": "Arbitrary injected projection.",
    }

    for row in (
        replace(ready, analysis_json=corrupt_shared_json),
        replace(ready, view_json=corrupt_projection),
        replace(ready, view_fact_snapshot_sha256="f" * 64),
        replace(ready, view_projection_identity_sha256="i" * 64),
        replace(ready, current_projection_manifest_sha256="m" * 64),
    ):
        report = _report_response(_context(), row, include_trace=False)
        assert report.state == ProductReportState.STALE
        assert report.projection is None


def test_projection_only_refresh_uses_current_role_identity_not_old_provenance(
) -> None:
    ready = _compatible_ready_row()
    assert ready.analysis_json is not None
    historical_manifest = {
        "schema_version": "role_projection_runtime_manifest.v1",
        "projection_schema": "role_projection.v1",
        "projection_policy_version": "historical-policy.v0",
        "projector_version": "historical-projector.v0",
        "safety_notices_sha256": "a" * 64,
    }
    historical_analysis = {
        **ready.analysis_json,
        "projection_manifest": historical_manifest,
        "projection_manifest_sha256": stable_hash(historical_manifest),
        "projection_identities": {
            "elder": "a" * 64,
            "family": "b" * 64,
            "doctor": "c" * 64,
        },
    }

    report = _report_response(
        _context(),
        replace(ready, analysis_json=historical_analysis),
        include_trace=False,
    )

    assert report.state == ProductReportState.READY
    assert report.projection is not None


def test_narrative_render_identity_uses_validated_projection_identity() -> None:
    ready = _compatible_ready_row()
    assert ready.analysis_json is not None
    shared = SharedNightAnalysis.model_validate(
        ready.analysis_json["shared_analysis"]
    )
    assert ready.view_projection_identity_sha256 is not None
    expected_render_identity = stable_hash(
        {
            "schema_version": "elder_narrative_request.v1",
            "shared_analysis_sha256": shared.shared_analysis_sha256,
            "elder_projection_sha256": (
                ready.view_projection_identity_sha256
            ),
            "render_manifest_sha256": (
                ready.current_narrative_manifest_sha256
            ),
        }
    )
    row = replace(
        ready,
        narrative_status="pending",
        narrative_operation_json={
            "narrative_manifest_sha256": (
                ready.current_narrative_manifest_sha256
            ),
            "projection_manifest_sha256": (
                ready.current_projection_manifest_sha256
            ),
            "shared_analysis_sha256": shared.shared_analysis_sha256,
            "elder_projection_sha256": (
                ready.view_projection_identity_sha256
            ),
            "elder_projection_content_sha256": (
                RoleProjection.model_validate(ready.view_json).projection_sha256
            ),
            "elder_projection": ready.view_json,
            "render_identity_sha256": expected_render_identity,
        },
    )

    report = _report_response(_context(), row, include_trace=False)

    assert report.state == ProductReportState.READY
    assert report.narrative is not None
    assert report.narrative.state == ProductNarrativeState.PENDING


def test_trace_aggregates_shared_and_narrative_usage_without_ids() -> None:
    row = replace(
        _compatible_ready_row(),
        narrative_status="succeeded",
        narrative_json={
            "narrative": {
                "schema_version": "elder_narrative.v1",
                "state": "fallback",
                "text": "Internal fallback text remains unpublished.",
                "failure_codes": ["NARRATIVE_BINDING_FAILED"],
            },
            "provider_usage": {
                "call_count": 1,
                "input_tokens": 30,
                "output_tokens": 10,
                "request_ids_present": True,
            },
        },
    )

    report = _report_response(_context(), row, include_trace=True)

    assert report.trace is not None
    assert report.trace.provider_call_count == 3
    assert report.trace.provider_input_tokens == 130
    assert report.trace.provider_output_tokens == 50
    assert report.trace.provider_request_ids_present is True
    assert report.narrative is not None
    assert report.narrative.state == ProductNarrativeState.FALLBACK
    assert report.narrative.text is None


def test_trace_attributes_legacy_narrative_to_shared_creator_without_ids() -> None:
    row = replace(
        _compatible_ready_row(),
        narrative_operation_id="internal-narrative-operation",
    )

    trace = _report_trace(
        row,
        quality=ProductReportQuality.GOOD,
        narrative=ProductReportNarrative(
            state=ProductNarrativeState.READY,
            text="Safe elder narrative.",
        ),
    )

    assert trace.elder_narrative == "created"
    assert "internal-narrative-operation" not in trace.model_dump_json()


def test_trace_attributes_bound_narrative_reuse_without_ids() -> None:
    ready = _compatible_ready_row()
    assert ready.request_json is not None
    result = dict(ready.request_json["report_result"])
    result.update(
        {
            "shared_operation_created": False,
            "elder_narrative_operation_id": "internal-narrative-operation",
            "elder_narrative_operation_created": False,
        }
    )
    row = replace(
        ready,
        request_json={"report_result": result},
        narrative_operation_id="internal-narrative-operation",
    )

    trace = _report_trace(
        row,
        quality=ProductReportQuality.GOOD,
        narrative=ProductReportNarrative(
            state=ProductNarrativeState.READY,
            text="Safe elder narrative.",
        ),
    )

    assert trace.elder_narrative == "reused"
    assert "internal-narrative-operation" not in trace.model_dump_json()


def test_trace_sums_all_bound_failed_attempts_and_committed_usage() -> None:
    def failed_attempt(
        *,
        operation_id: str,
        operation_type: str,
        call_count: int,
        input_tokens: int,
        output_tokens: int,
        request_ids_present: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": "product_provider_failed_attempt.v1",
            "operation_id": operation_id,
            "operation_type": operation_type,
            "failure_code": "provider_attempt_failed",
            "outcome_unknown": call_count > 0,
            "provider_usage": {
                "call_count": call_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "request_ids_present": request_ids_present,
                "failure_count": 1,
            },
        }

    row = replace(
        _compatible_ready_row(),
        shared_failed_attempts=(
            failed_attempt(
                operation_id="internal-shared-attempt-1",
                operation_type="product.shared_analysis.v1",
                call_count=1,
                input_tokens=10,
                output_tokens=2,
                request_ids_present=False,
            ),
            failed_attempt(
                operation_id="internal-shared-attempt-2",
                operation_type="product.shared_analysis.v1",
                call_count=2,
                input_tokens=20,
                output_tokens=4,
                request_ids_present=True,
            ),
        ),
        narrative_failed_attempts=(
            failed_attempt(
                operation_id="internal-narrative-attempt",
                operation_type="product.elder_narrative.v1",
                call_count=0,
                input_tokens=0,
                output_tokens=0,
                request_ids_present=False,
            ),
        ),
    )

    trace = _report_trace(
        row,
        quality=ProductReportQuality.GOOD,
        narrative=None,
    )
    serialized = trace.model_dump_json()

    # The compatible committed shared artifact contributes 2/100/40.
    assert trace.provider_call_count == 5
    assert trace.provider_input_tokens == 130
    assert trace.provider_output_tokens == 46
    assert trace.provider_request_ids_present is True
    assert "internal-shared-attempt" not in serialized
    assert "internal-narrative-attempt" not in serialized
    assert "failure_count" not in serialized


def test_final_fence_failure_keeps_prepared_provider_usage_in_trace() -> None:
    row = _report_row(
        request_status="succeeded",
        request_json={
            "report_result": {
                "schema_version": "product_report_request_result.v1",
                "state": "pending",
                "gate": "analyzable",
                "shared_operation_id": "internal-shared-operation",
                "shared_operation_created": True,
            }
        },
        shared_status="failed",
        shared_failed_attempts=(
            {
                "schema_version": "product_agent_prepared_attempt.v3",
                "operation_id": "internal-shared-operation",
                "product_attempt_id": "internal-prepared-attempt",
                "provider_usage": {
                    "call_count": 3,
                    "input_tokens": 75,
                    "output_tokens": 25,
                    "request_ids_present": True,
                    "failure_count": 0,
                },
            },
        ),
    )

    report = _report_response(_context(), row, include_trace=True)
    serialized = report.model_dump_json()

    assert report.state == ProductReportState.FAILED
    assert report.trace is not None
    assert report.trace.provider_call_count == 3
    assert report.trace.provider_input_tokens == 75
    assert report.trace.provider_output_tokens == 25
    assert report.trace.provider_request_ids_present is True
    assert "internal-shared-operation" not in serialized
    assert "internal-prepared-attempt" not in serialized


def test_orphaned_success_journal_usage_is_counted_without_identifiers() -> None:
    row = replace(
        _compatible_ready_row(),
        shared_orphaned_journal_usage=(
            {
                "call_count": 1,
                "input_tokens": 12,
                "output_tokens": 4,
                "request_ids_present": False,
                "failure_count": 0,
            },
        ),
        narrative_orphaned_journal_usage=(
            {
                "call_count": 1,
                "input_tokens": 8,
                "output_tokens": 3,
                "request_ids_present": True,
                "failure_count": 0,
            },
        ),
    )

    trace = _report_trace(
        row,
        quality=ProductReportQuality.GOOD,
        narrative=None,
    )
    serialized = trace.model_dump_json()

    # The compatible committed shared artifact contributes 2/100/40.
    assert trace.provider_call_count == 4
    assert trace.provider_input_tokens == 120
    assert trace.provider_output_tokens == 47
    assert trace.provider_request_ids_present is True
    assert "operation_id" not in serialized
    assert "product_attempt_id" not in serialized


def test_elder_narrative_manifest_drift_does_not_stale_shared_report() -> None:
    ready = _compatible_ready_row()
    assert ready.analysis_json is not None
    shared = SharedNightAnalysis.model_validate(
        ready.analysis_json["shared_analysis"]
    )
    assert ready.view_projection_identity_sha256 is not None
    old_manifest_sha256 = "o" * 64
    old_render_identity = stable_hash(
        {
            "schema_version": "elder_narrative_request.v1",
            "shared_analysis_sha256": shared.shared_analysis_sha256,
            "elder_projection_sha256": (
                ready.view_projection_identity_sha256
            ),
            "render_manifest_sha256": old_manifest_sha256,
        }
    )
    row = replace(
        ready,
        narrative_status="succeeded",
        narrative_operation_json={
            "narrative_manifest_sha256": old_manifest_sha256,
            "shared_analysis_sha256": shared.shared_analysis_sha256,
            "elder_projection_sha256": (
                ready.view_projection_identity_sha256
            ),
            "render_identity_sha256": old_render_identity,
        },
        current_narrative_manifest_sha256="n" * 64,
    )

    report = _report_response(_context(), row, include_trace=False)

    assert report.state == ProductReportState.READY
    assert report.narrative is not None
    assert report.narrative.state == ProductNarrativeState.STALE
