from __future__ import annotations

import inspect
import json
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest
import sleepagent.workers.product as product_module

from sleepagent.domain.contracts import AnalysisRevision, DataMode, DataSufficiency
from sleepagent.domain.habit import HabitFact, HabitOperation, HabitProfileState
from sleepagent.persistence.uow import UowScope
from sleepagent.runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeType,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    TrustLabel,
    stable_hash,
)
from sleepagent.runtime.factory import build_product_runtime_bundle
import sleepagent.runtime.provider as provider_module
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.memory import (
    GovernedMemoryState,
    MemoryItemStatus,
    MemoryPurpose,
    MemoryReadReceipt,
    MemorySliceItem,
    MemoryValueSchema,
    ProvenanceType,
)
from sleepagent.runtime.reports import (
    ElderNarrative,
    ElderNarrativeState,
    ReportRole,
    SharedNightAnalysis,
    build_role_projection_runtime_manifest,
    role_projection_identity_sha256,
)
from sleepagent.runtime.results import (
    PinnedPersonalizationContext,
    ProductEpisodeRunRequest,
    product_episode_request_hash,
    stable_selected_personalization_projection,
)
from sleepagent.workers.product import (
    LoadedProductAgentSource,
    PostgresProductAgentRepository,
    PreparedProductAgentArtifact,
    PreparedElderNarrativeArtifact,
    FailedProductProviderAttempt,
    ProductAgentConflict,
    ProductAgentLease,
    ProductAgentStaleSource,
    ProductAgentProcessor,
    ProductProviderUsage,
    ProductAgentWorkHandlerAdapter,
    _ProductProviderUsageObserver,
    _elder_narrative_identity_sha256,
    _elder_narrative_runtime_manifest,
    _governed_provider_request_records_for_narrative,
    _governed_provider_request_records_for_shared,
    _shared_runtime_manifest,
)
from sleepagent.workers.runtime import (
    InvocationRecord,
    InvocationState,
    LeaseClaim,
    OutcomeUnknownError,
    RetryableWorkError,
    WorkContext,
    WorkDisposition,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


class _SemanticModel:
    provider = "test-provider"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def generate(self, **kwargs: Any) -> Any:
        raise AssertionError(f"manifest test invoked model: {kwargs}")


def _source() -> LoadedProductAgentSource:
    return cast(
        LoadedProductAgentSource,
        SimpleNamespace(
            subject_id="subject-worker-test",
            facts=SimpleNamespace(data_mode=DataMode.REPLAY),
        ),
    )


def _bundle(*, narrative_model_id: str):
    analysis_model = _SemanticModel("analysis-model")
    return build_product_runtime_bundle(
        sleepcare_model=_SemanticModel(narrative_model_id),
        sleepcare_planning_model=_SemanticModel("planning-model"),
        evidence_reasoning_model=analysis_model,
        care_strategy_model=analysis_model,
        safety_review_model=analysis_model,
        sleepcare_content_plan_assembly=True,
    )


def test_narrative_model_change_does_not_change_shared_analysis_manifest() -> None:
    source = _source()
    first = _bundle(narrative_model_id="narrative-a")
    second = _bundle(narrative_model_id="narrative-b")

    first_shared = _shared_runtime_manifest(first, source=source)
    second_shared = _shared_runtime_manifest(second, source=source)
    first_narrative = _elder_narrative_runtime_manifest(first, source=source)
    second_narrative = _elder_narrative_runtime_manifest(second, source=source)

    assert first_shared == second_shared
    assert first_shared["sleepcare_control"]["model"]["model_id"] == (
        "planning-model"
    )
    assert set(first_shared["agents"]) == {
        "evidence_reasoning",
        "care_strategy",
        "safety_review",
    }
    assert "explain_for_elder" not in str(first_shared)
    assert first_narrative != second_narrative
    assert first_narrative["skill"]["skill_id"] == "explain_for_elder"
    projection_manifest = build_role_projection_runtime_manifest()
    assert projection_manifest["elder_presentation_policy_version"] == (
        "bounded_semantic_elder_atoms.v1"
    )
    assert "elder_presentation_policy_version" not in str(first_shared)

    shared_sha256 = stable_hash("committed-shared")
    projection_sha256 = stable_hash("elder-projection")
    first_identity = _elder_narrative_identity_sha256(
        shared_analysis_sha256=shared_sha256,
        elder_projection_sha256=projection_sha256,
        narrative_manifest_sha256=stable_hash(first_narrative),
    )
    second_identity = _elder_narrative_identity_sha256(
        shared_analysis_sha256=shared_sha256,
        elder_projection_sha256=projection_sha256,
        narrative_manifest_sha256=stable_hash(second_narrative),
    )
    assert first_identity != second_identity


class _ReadyReuseRepository(PostgresProductAgentRepository):
    def __init__(self) -> None:
        super().__init__(
            cast(Any, object()),
            UowScope(
                namespace_id="replay:worker-test",
                data_mode="replay",
                process_role="worker",
                purpose="worker",
                service_principal_id="worker-test",
                namespace_generation=1,
                run_id="run-test",
                arm_id="arm-test",
                subject_id="subject-test",
                authorization_epoch=1,
                privacy_epoch=1,
                retrieval_policy_epoch=1,
                worker_instance="worker-test",
            ),
        )
        self.loaded = False
        self.reserved_manifest: Mapping[str, Any] | None = None
        self.reserved_manifest_sha256: str | None = None

    def _load_committed_v3_shared_attempt(
        self,
        cursor: Any,
        *,
        operation_id: str,
        operation_json: Mapping[str, Any],
    ) -> PreparedProductAgentArtifact:
        del cursor, operation_json
        assert operation_id == "shared-operation"
        self.loaded = True
        return cast(
            PreparedProductAgentArtifact,
            SimpleNamespace(shared_analysis=object()),
        )

    def _reserve_elder_narrative(self, cursor: Any, **kwargs: Any):
        del cursor
        self.reserved_manifest = kwargs["narrative_manifest"]
        self.reserved_manifest_sha256 = kwargs[
            "narrative_manifest_sha256"
        ]
        return "narrative-operation", True

    def _refresh_role_projections_for_succeeded_shared(
        self,
        cursor: Any,
        **kwargs: Any,
    ):
        del cursor, kwargs
        projections = tuple(
            SimpleNamespace(role=role) for role in ReportRole
        )
        return projections, {
            role.value: stable_hash(f"projection:{role.value}")
            for role in ReportRole
        }


def test_ready_shared_reuse_reserves_current_prompt_only_narrative_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _ReadyReuseRepository()
    current_manifest = {"schema_version": "elder_narrative_runtime_manifest.v1", "prompt": "v2"}
    current_manifest_sha256 = stable_hash(current_manifest)
    projection_manifest = {
        "schema_version": "role_projection_runtime_manifest.v1"
    }
    monkeypatch.setattr(
        product_module,
        "build_elder_message_atoms",
        lambda *_: (),
    )
    source = cast(
        LoadedProductAgentSource,
        SimpleNamespace(
            facts=SimpleNamespace(
                elder_presentation_facts=lambda: object(),
            )
        ),
    )

    result = repository._reserve_narrative_for_succeeded_shared(
        cast(Any, object()),
        shared_operation_id="shared-operation",
        shared_operation_json={"result": "already-committed"},
        source_operation_json={
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        },
        wake_date=date(2026, 8, 27),
        reserved_at=datetime(2026, 8, 27, tzinfo=UTC),
        projection_manifest=projection_manifest,
        projection_manifest_sha256=stable_hash(projection_manifest),
        narrative_manifest=current_manifest,
        narrative_manifest_sha256=current_manifest_sha256,
        source=source,
    )

    assert result == ("narrative-operation", True)
    assert repository.loaded is True
    assert repository.reserved_manifest == current_manifest
    assert repository.reserved_manifest_sha256 == current_manifest_sha256


class _CheckpointContext:
    def __init__(self) -> None:
        self.checkpoints = 0

    def checkpoint(self, checkpoint_type: str, payload: Mapping[str, Any]) -> None:
        assert checkpoint_type == "product_provider_send"
        assert payload["attempt"] in {1, 2}
        self.checkpoints += 1


def test_provider_second_send_stops_when_exact_source_fence_changes() -> None:
    context = _CheckpointContext()
    source_state = {"revision": "revision-1"}

    def revalidate_source() -> None:
        if source_state["revision"] != "revision-1":
            raise ProductAgentStaleSource("source revision changed")

    observer = _ProductProviderUsageObserver(
        cast(Any, context),
        source_fence=revalidate_source,
    )
    observer.before_request(model="model", messages=[], attempt=1)
    source_state["revision"] = "revision-2"

    with pytest.raises(ProductAgentStaleSource, match="source revision changed"):
        observer.before_request(model="model", messages=[], attempt=2)

    assert context.checkpoints == 2
    assert observer.snapshot().call_count == 1


def _provider_invocation(*, request_id: str) -> AgentInvocationRecord:
    return AgentInvocationRecord(
        invocation_id="agent-invocation-1",
        episode_id="episode-1",
        agent_id=AgentId.EVIDENCE_REASONING,
        agent_version="agent.v1",
        profile_version="profile.v1",
        profile_hash="a" * 64,
        skill_id="reason_over_evidence",
        skill_version="skill.v1",
        skill_package_hash="b" * 64,
        skill_lock_hash="c" * 64,
        prompt_bundle_hash="d" * 64,
        schema_version="output.v1",
        prompt_version="prompt.v1",
        policy_version="policy.v1",
        context_packet_id="context-1",
        context_hash="e" * 64,
        target_hash="f" * 64,
        provider="provider",
        model_id="model",
        provider_request_id=request_id,
        provider_input_tokens=12,
        started_at=datetime(2026, 8, 27, tzinfo=UTC),
        ended_at=datetime(2026, 8, 27, tzinfo=UTC),
        latency_ms=1,
        validation_status="runtime_validated",
        safe_summary="safe",
    )


def test_full_provider_request_id_is_journal_only_not_artifact_json() -> None:
    request_id = "provider-request-secret-1"
    record = _provider_invocation(request_id=request_id)
    shared = SharedNightAnalysis.model_construct(
        agent_invocations=(record,),
        tool_receipts=(),
        envelopes=(),
    )
    shared_artifact = PreparedProductAgentArtifact.model_construct(
        schema_version="product_agent_prepared_attempt.v3",
        shared_analysis=shared,
        role_runs=(),
    )
    narrative = ElderNarrative.model_construct(
        invocation=record.model_copy(update={"agent_id": AgentId.SLEEP_CARE}),
    )
    narrative_artifact = PreparedElderNarrativeArtifact.model_construct(
        narrative=narrative,
    )

    shared_journal = _governed_provider_request_records_for_shared(
        shared_artifact
    )
    narrative_journal = _governed_provider_request_records_for_narrative(
        narrative_artifact
    )

    assert shared_journal[0]["provider_request_id"] == request_id
    assert narrative_journal[0]["provider_request_id"] == request_id
    assert request_id not in json.dumps(
        shared_artifact.model_dump(mode="json"),
        sort_keys=True,
    )
    assert request_id not in json.dumps(
        narrative_artifact.model_dump(mode="json"),
        sort_keys=True,
    )
    store = _AdapterStore()
    context = WorkContext(
        _adapter_claim(),
        cast(Any, store),
        threading.Event(),
    )
    response = context.invocation_dispatcher().dispatch(
        invocation_key="governed-request-id-test",
        request={"schema_version": "request.v1"},
        sender=lambda: (
            {
                "artifact": shared_artifact.model_dump(mode="json"),
                "governed_provider_request_records": shared_journal,
            },
            None,
        ),
    )
    durable = store.invocations["governed-request-id-test"]
    assert durable.state is InvocationState.RESPONSE_RECEIVED
    assert durable.response == response
    assert durable.response["governed_provider_request_records"][0][
        "provider_request_id"
    ] == request_id
    assert request_id not in json.dumps(
        durable.response["artifact"],
        sort_keys=True,
    )


class _AdapterStore:
    def __init__(self) -> None:
        self.invocations: dict[str, InvocationRecord] = {}

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        return UowScope(
            namespace_id=claim.namespace_id,
            data_mode="replay",
            process_role="worker",
            purpose="worker",
            service_principal_id="worker-test",
            namespace_generation=1,
            run_id=claim.run_id,
            arm_id=claim.arm_id,
            subject_id=claim.subject_id,
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
            worker_instance=claim.worker_instance,
        )

    def checkpoint(self, claim: LeaseClaim, **kwargs: Any) -> bool:
        del claim, kwargs
        return True

    def reserve_invocation(
        self,
        claim: LeaseClaim,
        *,
        invocation_kind: Any,
        invocation_key: str,
        request_sha256: str,
    ) -> InvocationRecord:
        record = InvocationRecord(
            invocation_id="outer-invocation-1",
            invocation_key=invocation_key,
            invocation_kind=invocation_kind,
            work_id=claim.work_id,
            lease_generation=claim.lease_generation,
            request_sha256=request_sha256,
            state=InvocationState.RESERVED,
        )
        self.invocations[invocation_key] = record
        return record

    def mark_invocation_send_started(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
    ) -> bool:
        del claim
        self.invocations[record.invocation_key] = record.model_copy(
            update={"state": InvocationState.SEND_STARTED}
        )
        return True

    def finalize_invocation(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
        *,
        state: InvocationState,
        provider_request_id: str | None,
        response: Mapping[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        del claim
        self.invocations[record.invocation_key] = record.model_copy(
            update={
                "state": state,
                "provider_request_id": provider_request_id,
                "response": response,
                "error_code": error_code,
            }
        )
        return True


def _adapter_claim(
    *,
    attempt: int = 1,
    max_attempts: int = 5,
    operation_type: str = "product.shared_analysis.v1",
) -> LeaseClaim:
    authorization_snapshot = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": "worker-test",
        "namespace_id": "replay:worker-test",
        "namespace_generation": 1,
        "data_mode": "replay",
        "run_id": "run-test",
        "arm_id": "arm-test",
        "subject_id": "subject-test",
        "purpose": "worker",
        "allowed_handler": "product_agent",
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_policy_epoch": 1,
    }
    return LeaseClaim(
        work_id="shared-operation",
        operation_id="shared-operation",
        queue="product_agent",
        namespace_id="replay:worker-test",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-test",
        arm_id="arm-test",
        subject_id="subject-test",
        operation_version=0,
        lease_generation=1,
        fencing_token="f" * 64,
        worker_instance="worker-test",
        attempt=attempt,
        max_attempts=max_attempts,
        lease_deadline=datetime(2026, 8, 27, 1, tzinfo=UTC),
        payload={},
        authorization_snapshot=authorization_snapshot,
        metadata={
            "work_kind": "operation",
            "operation_type": operation_type,
            "queue_name": "product_agent",
        },
    )


class _FailingProviderProcessor(ProductAgentProcessor):
    def __init__(self, *, provider_started: bool) -> None:
        self.provider_started = provider_started
        self.now_factory = lambda: datetime(2026, 8, 27, tzinfo=UTC)
        self.failed_usage = None
        self.compatibility_failures: list[tuple[str, bool]] = []

    def load_source(self, scope: UowScope, lease: Any) -> Any:
        del scope, lease
        return SimpleNamespace(
            operation_id="shared-operation",
            night_episode_id="night-1",
            night_episode_revision_id="revision-1",
            night_episode_revision_number=1,
            observation_set_sha256="a" * 64,
            policy_sha256="b" * 64,
            operation_json={
                "desired_analysis_sha256": "c" * 64,
                "invocation_generation": 1,
            },
        )

    def revalidate_provider_source_fence(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def prepare_shared(self, **kwargs: Any) -> Any:
        del kwargs
        if self.provider_started:
            observer = provider_module._PROVIDER_TRANSPORT_AUDIT_OBSERVER.get()
            assert observer is not None
            observer.before_request(
                model="provider-model",
                messages=[{"role": "user", "content": "safe"}],
                attempt=1,
            )
            observer.after_failure(
                model="provider-model",
                attempt=1,
                error_type="ProviderError",
                latency_ms=2,
            )
        raise RuntimeError("provider preparation failed")

    def persist_failed_provider_attempt(self, *args: Any, **kwargs: Any) -> None:
        del args
        self.failed_usage = kwargs["provider_usage"]

    def fail_compatibility_bridge(self, *args: Any, **kwargs: Any) -> None:
        del args
        self.compatibility_failures.append(
            (kwargs["error_code"], kwargs["outcome_unknown"])
        )


def test_provider_exception_persists_exact_failed_attempt_usage() -> None:
    processor = _FailingProviderProcessor(provider_started=True)
    context = WorkContext(
        _adapter_claim(),
        cast(Any, _AdapterStore()),
        threading.Event(),
    )

    with pytest.raises(OutcomeUnknownError, match="connector_outcome_unknown"):
        ProductAgentWorkHandlerAdapter(processor=processor)(context)

    assert processor.failed_usage.model_dump(mode="json") == {
        "call_count": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "request_ids_present": False,
        "failure_count": 1,
    }
    assert processor.compatibility_failures == [
        ("connector_outcome_unknown", True)
    ]


def test_zero_call_failure_is_known_not_sent_and_keeps_wrapper_retryable() -> None:
    processor = _FailingProviderProcessor(provider_started=False)
    context = WorkContext(
        _adapter_claim(),
        cast(Any, _AdapterStore()),
        threading.Event(),
    )

    with pytest.raises(
        RetryableWorkError,
        match="product_provider_known_not_sent",
    ):
        ProductAgentWorkHandlerAdapter(processor=processor)(context)

    assert processor.failed_usage.call_count == 0
    assert processor.failed_usage.failure_count == 0
    assert processor.compatibility_failures == []


class _NarrativeKnownNotSentProcessor(_FailingProviderProcessor):
    def __init__(self, *, generation: int) -> None:
        super().__init__(provider_started=False)
        self.generation = generation

    def load_source(self, scope: UowScope, lease: Any) -> Any:
        del scope, lease
        return SimpleNamespace(
            operation_id="shared-operation",
            night_episode_id="night-1",
            night_episode_revision_id="revision-1",
            night_episode_revision_number=1,
            observation_set_sha256="a" * 64,
            policy_sha256="b" * 64,
            operation_json={
                "render_identity_sha256": "c" * 64,
                "shared_analysis_sha256": "d" * 64,
                "elder_projection_sha256": "e" * 64,
                "narrative_manifest_sha256": "f" * 64,
                "invocation_generation": self.generation,
            },
        )

    def load_committed_shared_for_narrative(self, *args: Any) -> Any:
        del args
        return object()

    def prepare_elder_narrative(self, **kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError("known not sent")


def test_narrative_generation_changes_outer_invocation_key() -> None:
    keys: list[str] = []
    for generation in (1, 2):
        processor = _NarrativeKnownNotSentProcessor(generation=generation)
        store = _AdapterStore()
        context = WorkContext(
            _adapter_claim(
                operation_type="product.elder_narrative.v1"
            ),
            cast(Any, store),
            threading.Event(),
        )
        with pytest.raises(RetryableWorkError):
            ProductAgentWorkHandlerAdapter(processor=processor)(context)
        keys.extend(store.invocations)

    assert len(keys) == 2
    assert ":g1:" in keys[0]
    assert ":g2:" in keys[1]
    assert keys[0] != keys[1]


class _NarrativeCommitRepository:
    def __init__(self) -> None:
        self.projection_manifest: Mapping[str, Any] | None = None

    def persist_elder_narrative_prepared(self, *args: Any) -> None:
        del args

    def commit_elder_narrative_prepared(self, *args: Any, **kwargs: Any) -> Any:
        del args
        self.projection_manifest = kwargs["projection_manifest"]
        return SimpleNamespace(state="fallback")


def test_narrative_commit_supplies_current_projection_manifest() -> None:
    repository = _NarrativeCommitRepository()
    processor = ProductAgentProcessor(
        cast(Any, _GateUowFactory()),
        runtime_bundle=_bundle(narrative_model_id="narrative-model"),
        now_factory=lambda: datetime(2026, 8, 27, tzinfo=UTC),
        repository_factory=lambda connection, scope: repository,
    )

    result = processor.persist_and_commit_elder_narrative(
        _recovery_scope(),
        _recovery_lease(),
        PreparedElderNarrativeArtifact.model_construct(),
        source=_source(),
        committed_shared=PreparedProductAgentArtifact.model_construct(),
    )

    assert result.state == "fallback"
    assert repository.projection_manifest == (
        build_role_projection_runtime_manifest()
    )


class _RecoveryCursor:
    def __init__(self, *, invocation_states: tuple[str, ...]) -> None:
        self.invocation_states = invocation_states
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._one: Any = None
        self._all: list[Any] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = 0
        self._one = None
        self._all = []
        if normalized.startswith(
            "SELECT operation_id, status, operation_json, attempt_count"
        ):
            self._one = (
                "canonical-shared",
                "dead_letter",
                {
                    "schema_version": "backend_operation.v2",
                    "invocation_generation": 1,
                },
                7,
            )
        elif "SELECT EXISTS" in normalized:
            self._one = (False,)
        elif normalized.startswith("SELECT current_state"):
            self._all = [(value,) for value in self.invocation_states]
        elif normalized.startswith("UPDATE public.sleep_domain_operations"):
            self.rowcount = 1

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._all

    def close(self) -> None:
        return None


class _RecoveryConnection:
    def __init__(self, cursor: _RecoveryCursor) -> None:
        self.value = cursor

    def cursor(self) -> _RecoveryCursor:
        return self.value


class _RecoveryRepository(PostgresProductAgentRepository):
    def _lock_report_request_fence(
        self,
        cursor: Any,
        lease: ProductAgentLease,
    ) -> dict[str, Any]:
        del cursor, lease
        return {
            "schema_version": "backend_operation.v2",
            "night_episode_revision_id": "revision-1",
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        }

    def revalidate_provider_source_fence(self, *args: Any, **kwargs: Any):
        del args, kwargs
        return (
            HabitProfileState(subject_id="subject-test"),
            GovernedMemoryState(subject_id="subject-test"),
        )


def _recovery_scope() -> UowScope:
    return UowScope(
        namespace_id="replay:worker-test",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="worker-test",
        namespace_generation=1,
        run_id="run-test",
        arm_id="arm-test",
        subject_id="subject-test",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-test",
    )


def _recovery_source() -> Any:
    return SimpleNamespace(
        operation_id="report-request",
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        night_episode_revision_number=1,
        observation_set_sha256="a" * 64,
        policy_sha256="b" * 64,
        policy_versions={"quality": "v1", "risk": "v1"},
        operation_json={
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        },
        facts=SimpleNamespace(
            local_sleep_date="2026-08-27",
            canonical_data_version="canonical-v1",
            provider_quality_summary=lambda: {
                "quality_state": "good",
                "data_sufficiency": "sufficient",
            },
            provider_risk_summary=lambda: {
                "risk_state": "no_reviewed_signal",
                "health_escalation_allowed": False,
            },
        ),
    )


def _recovery_lease() -> ProductAgentLease:
    return ProductAgentLease(
        operation_id="report-request",
        attempt_sequence=1,
        lease_generation=1,
        fencing_token="f" * 64,
        worker_instance="worker-test",
    )


def _reserve_recovery(
    repository: PostgresProductAgentRepository,
) -> Any:
    return repository.reserve_shared_analysis(
        _recovery_lease(),
        source=_recovery_source(),
        desired_analysis_sha256="e" * 64,
        consumed_context_sha256="c" * 64,
        runtime_manifest={"schema_version": "shared.v1"},
        runtime_manifest_sha256="d" * 64,
        projection_manifest={"schema_version": "projection.v1"},
        projection_manifest_sha256=stable_hash(
            {"schema_version": "projection.v1"}
        ),
        narrative_manifest={"schema_version": "narrative.v1"},
        narrative_manifest_sha256=stable_hash(
            {"schema_version": "narrative.v1"}
        ),
        routed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


def test_known_failed_dead_letter_revives_with_new_generation_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _RecoveryCursor(invocation_states=("known_failed",))
    repository = _RecoveryRepository(
        cast(Any, _RecoveryConnection(cursor)),
        _recovery_scope(),
    )
    monkeypatch.setattr(
        product_module,
        "_recompute_shared_identity",
        lambda **kwargs: ("c" * 64, "d" * 64, "e" * 64),
    )

    result = _reserve_recovery(repository)

    reset = next(
        params
        for sql, params in cursor.calls
        if "dead_lettered_at = NULL" in sql
    )
    assert json.loads(reset[2])["invocation_generation"] == 2
    assert reset[3] == 12
    assert result.state == "pending"
    assert result.shared_operation_id == "canonical-shared"


def test_outcome_unknown_canonical_work_is_never_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _RecoveryCursor(invocation_states=("outcome_unknown",))
    repository = _RecoveryRepository(
        cast(Any, _RecoveryConnection(cursor)),
        _recovery_scope(),
    )
    monkeypatch.setattr(
        product_module,
        "_recompute_shared_identity",
        lambda **kwargs: ("c" * 64, "d" * 64, "e" * 64),
    )

    result = _reserve_recovery(repository)

    assert not any("dead_lettered_at = NULL" in sql for sql, _ in cursor.calls)
    assert result.state == "pending"
    assert result.shared_operation_created is False


def test_ready_reuse_rejects_context_change_inside_reservation_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _RecoveryCursor(invocation_states=())
    repository = _RecoveryRepository(
        cast(Any, _RecoveryConnection(cursor)),
        _recovery_scope(),
    )
    monkeypatch.setattr(
        product_module,
        "_recompute_shared_identity",
        lambda **kwargs: ("9" * 64, "d" * 64, "8" * 64),
    )

    with pytest.raises(
        ProductAgentStaleSource,
        match="changed before reservation",
    ):
        _reserve_recovery(repository)

    assert not any(
        sql.startswith(
            "SELECT operation_id, status, operation_json, attempt_count"
        )
        for sql, _ in cursor.calls
    )


class _GateUow:
    connection = object()

    def __enter__(self) -> "_GateUow":
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def commit(self) -> None:
        return None


class _GateUowFactory:
    def begin(self, scope: UowScope) -> _GateUow:
        del scope
        return _GateUow()


class _GateRepository:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.shared_reservations = 0

    def complete_report_request(self, *args: Any, **kwargs: Any) -> bool:
        del args
        self.states.append(kwargs["state"])
        return True

    def reserve_shared_analysis(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.shared_reservations += 1
        raise AssertionError("gated report reserved shared work")


class _GateProcessor(ProductAgentProcessor):
    def __init__(self, *, urgent: bool, sufficiency: str) -> None:
        self.repository = _GateRepository()
        self.uow_factory = _GateUowFactory()
        self.repository_factory = (
            lambda connection, scope: self.repository
        )
        self.now_factory = lambda: datetime(2026, 8, 27, tzinfo=UTC)
        self.source = SimpleNamespace(
            operation_id="report-request",
            facts=SimpleNamespace(
                data_sufficiency=sufficiency,
                provider_risk_summary=lambda: {
                    "health_escalation_allowed": urgent,
                },
                provider_quality_summary=lambda: {},
            ),
        )

    def load_source(self, scope: UowScope, lease: ProductAgentLease) -> Any:
        del scope, lease
        return self.source


@pytest.mark.parametrize(
    ("urgent", "sufficiency", "expected_state"),
    (
        (True, "sufficient", "urgent_handled"),
        (False, "unusable", "unusable_blocked"),
    ),
)
def test_report_gate_creates_zero_shared_work(
    urgent: bool,
    sufficiency: str,
    expected_state: str,
) -> None:
    processor = _GateProcessor(urgent=urgent, sufficiency=sufficiency)

    result = processor.route_report_request(
        _recovery_scope(),
        _recovery_lease(),
    )

    assert result.state == expected_state
    assert processor.repository.states == [expected_state]
    assert processor.repository.shared_reservations == 0


def test_final_shared_conflict_terminalizes_automatic_wrapper() -> None:
    class _ConflictProcessor(_FailingProviderProcessor):
        def load_source(self, scope: UowScope, lease: Any) -> Any:
            del scope, lease
            raise ProductAgentConflict("concurrent commit")

    processor = _ConflictProcessor(provider_started=False)
    result = ProductAgentWorkHandlerAdapter(processor=processor)(
        WorkContext(
            _adapter_claim(attempt=5, max_attempts=5),
            cast(Any, _AdapterStore()),
            threading.Event(),
        )
    )

    assert result.disposition is WorkDisposition.TERMINAL
    assert result.error_code == "product_agent_conflict_exhausted"
    assert processor.compatibility_failures == [
        ("product_agent_conflict_exhausted", False)
    ]


class _CompatibilityCursor:
    def __init__(self, compatibility_json: Mapping[str, Any]) -> None:
        self.compatibility_json = dict(compatibility_json)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = (
            1 if normalized.startswith("UPDATE public.sleep_domain_operations")
            else 0
        )

    def fetchone(self) -> Any:
        return ("pending", self.compatibility_json)


def test_shared_success_copies_exact_legacy_product_result() -> None:
    request_json = {
        "night_episode_revision_id": "revision-1",
        "compatibility_product_agent_operation_id": "legacy-wrapper",
    }
    compatibility_json = {
        "schema_version": "backend_operation.v2",
        "compatibility_mode": "shared_analysis_bridge.v1",
        "report_request_operation_id": "report-request",
        "night_episode_revision_id": "revision-1",
    }
    product_result = {
        "schema_version": "product_agent_result.v1",
        "product_attempt_id": "attempt-1",
        "analysis_revision_id": "analysis-1",
        "night_episode_revision_id": "revision-1",
        "role_view_ids": ["elder-view", "family-view", "doctor-view"],
        "analysis_status": "ready",
        "induction_operation_id": "induction-1",
        "induction_manifest_id": "manifest-1",
    }
    cursor = _CompatibilityCursor(compatibility_json)
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )

    repository._complete_request_compatibility(
        cast(Any, cursor),
        request_operation_id="report-request",
        request_json=request_json,
        product_result=product_result,
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    update_params = next(
        params
        for sql, params in cursor.calls
        if sql.startswith("UPDATE public.sleep_domain_operations")
    )
    assert json.loads(update_params[0])["result"] == product_result


class _FailedAttemptCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._one: Any = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SELECT operation_json, policy_sha256"):
            self._one = (
                {"night_episode_revision_id": "revision-1"},
                "b" * 64,
            )
        elif normalized.startswith(
            "SELECT product_attempt_id, attempt_state, query_visible"
        ):
            self._one = None
        elif normalized.startswith("INSERT INTO public.backend_product_attempts"):
            self._one = None
            self.rowcount = 1

    def fetchone(self) -> Any:
        return self._one

    def close(self) -> None:
        return None


class _FailedAttemptRepository(PostgresProductAgentRepository):
    def _lock_and_validate_epochs(self) -> tuple[int, int, int]:
        return (1, 1, 1)


@pytest.mark.parametrize(
    ("call_count", "expected_state"),
    ((0, "abandoned"), (1, "outcome_unknown")),
)
def test_failed_attempt_insert_is_private_typed_and_exactly_fenced(
    call_count: int,
    expected_state: str,
) -> None:
    cursor = _FailedAttemptCursor()
    repository = _FailedAttemptRepository(
        cast(Any, _RecoveryConnection(cast(Any, cursor))),
        _recovery_scope(),
    )
    lease = ProductAgentLease(
        operation_id="shared-operation",
        attempt_sequence=3,
        lease_generation=2,
        fencing_token="f" * 64,
        worker_instance="worker-test",
    )
    usage = ProductProviderUsage(
        call_count=call_count,
        input_tokens=11,
        output_tokens=7,
        request_ids_present=call_count > 0,
        failure_count=call_count,
    )
    attempt = FailedProductProviderAttempt(
        product_attempt_id="failed-attempt-3",
        operation_id="shared-operation",
        operation_type="product.shared_analysis.v1",
        night_episode_revision_id="revision-1",
        source_state_version=4,
        source_fact_snapshot_sha256="a" * 64,
        policy_sha256="b" * 64,
        attempt_sequence=3,
        provider_usage=usage,
        outcome_unknown=call_count > 0,
        failed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    repository.persist_failed_provider_attempt(lease, attempt)

    insert_sql, insert_params = next(
        (sql, params)
        for sql, params in cursor.calls
        if sql.startswith("INSERT INTO public.backend_product_attempts")
    )
    assert "%s, FALSE" in insert_sql
    assert insert_params[7] == "shared-operation"
    assert insert_params[8] == "revision-1"
    assert insert_params[9] == 3
    assert insert_params[10] == expected_state
    assert insert_params[13:16] == (1, 1, 1)
    persisted_json = json.loads(insert_params[20])
    assert persisted_json["schema_version"] == (
        "product_provider_failed_attempt.v1"
    )
    assert persisted_json["provider_usage"] == usage.model_dump(mode="json")
    assert persisted_json["operation_id"] == "shared-operation"
    assert persisted_json["night_episode_revision_id"] == "revision-1"


def _repair_v3_artifact(
    *,
    revision_number: int = 1,
    parent_analysis_revision_id: str | None = None,
    analysis_revision_id: str = "analysis-candidate",
    product_attempt_id: str = "attempt-candidate",
    operation_id: str = "shared-operation",
) -> PreparedProductAgentArtifact:
    analysis = AnalysisRevision.model_construct(
        analysis_revision_id=analysis_revision_id,
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        subject_id="subject-test",
        revision_number=revision_number,
        parent_analysis_revision_id=parent_analysis_revision_id,
    )
    return PreparedProductAgentArtifact.model_construct(
        schema_version="product_agent_prepared_attempt.v3",
        product_attempt_id=product_attempt_id,
        operation_id=operation_id,
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        source_state_version=1,
        source_fact_snapshot_sha256="a" * 64,
        policy_sha256="b" * 64,
        analysis=analysis,
        role_runs=(),
        prepared_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


class _RepairCursor:
    def __init__(self, *, prior: tuple[str, int] | None = None) -> None:
        self.prior = prior
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._one: Any = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = 0
        self._one = None
        if normalized.startswith("SELECT analysis_revision_id, revision_number"):
            self._one = self.prior
        elif normalized.startswith("UPDATE public.backend_product_attempts"):
            self.rowcount = 1
            if "RETURNING attempt_sequence" in normalized:
                self._one = (params[0], params[1], params[2])
        elif normalized.startswith("UPDATE public.sleep_domain_operations"):
            self.rowcount = 1

    def fetchone(self) -> Any:
        return self._one


def test_final_context_writer_locks_use_writer_keys_in_fixed_order() -> None:
    cursor = _RepairCursor()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )

    repository._lock_final_context_writers(cast(Any, cursor))

    assert [params[0].split(":")[1] for _, params in cursor.calls] == [
        "habit",
        "memory",
    ]
    assert all("hashtextextended(%s, 42)" in sql for sql, _ in cursor.calls)
    assert cursor.calls[0][1][0] == (
        "l2:habit:replay:worker-test:replay:1:run-test:arm-test:subject-test"
    )


def test_final_commit_paths_lock_l2_before_context_revalidation() -> None:
    shared_source = inspect.getsource(
        PostgresProductAgentRepository.commit_prepared
    )
    narrative_source = inspect.getsource(
        PostgresProductAgentRepository.commit_elder_narrative_prepared
    )

    assert shared_source.index("_lock_final_context_writers") < (
        shared_source.index("revalidate_provider_source_fence")
    )
    assert narrative_source.index("_lock_final_context_writers") < (
        narrative_source.index("revalidate_provider_source_fence")
    )
    assert "prepare_shared" not in shared_source
    assert "render_elder_narrative" not in narrative_source


class _L2LockCursor:
    def __init__(self, locks: Mapping[str, threading.Lock]) -> None:
        self.locks = locks
        self.acquired: list[threading.Lock] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert "hashtextextended(%s, 42)" in " ".join(sql.split())
        capability = params[0].split(":")[1]
        lock = self.locks[capability]
        assert lock.acquire(timeout=2)
        self.acquired.append(lock)

    def release_transaction(self) -> None:
        for lock in reversed(self.acquired):
            lock.release()


@pytest.mark.parametrize("capability", ("habit", "memory"))
def test_final_context_lock_serializes_matching_l2_writer(
    capability: str,
) -> None:
    locks = {"habit": threading.Lock(), "memory": threading.Lock()}
    cursor = _L2LockCursor(locks)
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    writer_acquired = threading.Event()

    repository._lock_final_context_writers(cast(Any, cursor))

    def writer() -> None:
        with locks[capability]:
            writer_acquired.set()

    thread = threading.Thread(target=writer)
    thread.start()
    assert writer_acquired.wait(timeout=0.05) is False
    cursor.release_transaction()
    assert writer_acquired.wait(timeout=1)
    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_publication_lock_is_scoped_to_exact_episode_revision() -> None:
    cursor = _RepairCursor()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )

    repository._lock_analysis_publication(
        cast(Any, cursor),
        _repair_v3_artifact(),
    )

    sql, params = cursor.calls[0]
    assert "hashtextextended(%s, 43)" in sql
    assert params[0].endswith(":subject-test:revision-1")


def test_shared_publication_rebases_only_revision_envelope() -> None:
    cursor = _RepairCursor(prior=("analysis-winner", 4))
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    artifact = _repair_v3_artifact()
    original_analysis_id = artifact.analysis.analysis_revision_id
    original_attempt_sha256 = artifact.attempt_sha256

    rebased = repository._rebase_shared_analysis_parent(
        cast(Any, cursor),
        _recovery_lease().model_copy(
            update={"operation_id": "shared-operation"}
        ),
        artifact,
    )

    assert rebased.analysis.analysis_revision_id == original_analysis_id
    assert rebased.analysis.revision_number == 5
    assert rebased.analysis.parent_analysis_revision_id == "analysis-winner"
    assert rebased.attempt_sha256 != original_attempt_sha256
    update = next(
        params
        for sql, params in cursor.calls
        if sql.startswith("UPDATE public.backend_product_attempts")
    )
    assert update[5] == original_attempt_sha256


def test_shared_publication_keeps_matching_envelope_without_rewrite() -> None:
    cursor = _RepairCursor(prior=("analysis-winner", 4))
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    artifact = _repair_v3_artifact(
        revision_number=5,
        parent_analysis_revision_id="analysis-winner",
    )

    same = repository._rebase_shared_analysis_parent(
        cast(Any, cursor),
        _recovery_lease(),
        artifact,
    )

    assert same is artifact
    assert not any(
        sql.startswith("UPDATE public.backend_product_attempts")
        for sql, _ in cursor.calls
    )


class _PublicationState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latest: tuple[str, int] | None = None
        self.committed: list[tuple[str, int, str | None]] = []


class _ConcurrentPublicationCursor(_RepairCursor):
    def __init__(self, state: _PublicationState) -> None:
        super().__init__()
        self.state = state

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        if "hashtextextended(%s, 43)" in normalized:
            self.calls.append((normalized, params))
            assert self.state.lock.acquire(timeout=2)
            self.rowcount = 1
            self._one = (None,)
            return
        if normalized.startswith("SELECT analysis_revision_id, revision_number"):
            self.calls.append((normalized, params))
            self._one = self.state.latest
            self.rowcount = 0
            return
        super().execute(sql, params)

    def publish(self, artifact: PreparedProductAgentArtifact) -> None:
        self.state.latest = (
            artifact.analysis.analysis_revision_id,
            artifact.analysis.revision_number,
        )
        self.state.committed.append(
            (
                artifact.analysis.analysis_revision_id,
                artifact.analysis.revision_number,
                artifact.analysis.parent_analysis_revision_id,
            )
        )
        self.state.lock.release()


def test_cross_identity_publication_reuses_precomputed_results_and_progresses(
) -> None:
    state = _PublicationState()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    artifacts = (
        _repair_v3_artifact(
            analysis_revision_id="analysis-a",
            product_attempt_id="attempt-a",
            operation_id="operation-a",
        ),
        _repair_v3_artifact(
            analysis_revision_id="analysis-b",
            product_attempt_id="attempt-b",
            operation_id="operation-b",
        ),
    )
    barrier = threading.Barrier(2)
    results: list[PreparedProductAgentArtifact] = []
    errors: list[BaseException] = []
    provider_calls = 2  # both different identities finished before publishing

    def publish(index: int) -> None:
        artifact = artifacts[index]
        lease = ProductAgentLease(
            operation_id=artifact.operation_id,
            attempt_sequence=1,
            lease_generation=1,
            fencing_token=str(index + 1) * 64,
            worker_instance=f"worker-{index}",
        )
        cursor = _ConcurrentPublicationCursor(state)
        publication_locked = False
        try:
            barrier.wait(timeout=2)
            repository._lock_analysis_publication(
                cast(Any, cursor), artifact
            )
            publication_locked = True
            rebased = repository._rebase_shared_analysis_parent(
                cast(Any, cursor), lease, artifact
            )
            cursor.publish(rebased)
            publication_locked = False
            results.append(rebased)
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)
            if publication_locked:
                state.lock.release()

    threads = [
        threading.Thread(target=publish, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(item.analysis.revision_number for item in results) == [1, 2]
    assert len({item.analysis.analysis_revision_id for item in results}) == 2
    second = next(item for item in results if item.analysis.revision_number == 2)
    first = next(item for item in results if item.analysis.revision_number == 1)
    assert second.analysis.parent_analysis_revision_id == (
        first.analysis.analysis_revision_id
    )
    assert provider_calls == 2


def test_journaled_v3_artifact_can_move_to_later_business_attempt() -> None:
    cursor = _RepairCursor()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    artifact = _repair_v3_artifact()
    lease = ProductAgentLease(
        operation_id="shared-operation",
        attempt_sequence=2,
        lease_generation=1,
        fencing_token="9" * 64,
        worker_instance="worker-test",
    )

    repository._persist_existing_artifact(
        cast(Any, cursor),
        lease,
        artifact,
        (1, 7, "8" * 64, True),
    )

    update = cursor.calls[0]
    assert update[1][:3] == (2, 1, "9" * 64)


def test_legacy_artifact_cannot_move_to_later_business_attempt() -> None:
    cursor = _RepairCursor()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )
    legacy = _repair_v3_artifact().model_copy(
        update={"schema_version": "product_agent_prepared_attempt.v2"}
    )
    lease = ProductAgentLease(
        operation_id="shared-operation",
        attempt_sequence=2,
        lease_generation=8,
        fencing_token="9" * 64,
        worker_instance="worker-test",
    )

    with pytest.raises(ProductAgentConflict, match="cannot be taken over"):
        repository._persist_existing_artifact(
            cast(Any, cursor),
            lease,
            legacy,
            (1, 7, "8" * 64, True),
        )


class _FinalGateCursor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        del params
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE public.sleep_domain_operations"):
            self.events.append("terminal_update")
            self.rowcount = 1

    def close(self) -> None:
        return None


class _FinalGateRepository(PostgresProductAgentRepository):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        super().__init__(
            cast(Any, _RecoveryConnection(cast(Any, _FinalGateCursor(events)))),
            _recovery_scope(),
        )

    def _lock_and_validate_epochs(self) -> tuple[int, int, int]:
        self.events.append("epochs")
        return (1, 1, 1)

    def _lock_report_request_fence(
        self,
        cursor: Any,
        lease: ProductAgentLease,
    ) -> dict[str, Any]:
        del cursor, lease
        self.events.append("operation")
        return {"night_episode_revision_id": "revision-1"}

    def _lock_current_report_gate(self, *args: Any, **kwargs: Any):
        del args, kwargs
        self.events.append("gate")
        return (
            "revision-1",
            "quality-1",
            SimpleNamespace(
                model_dump=lambda **kwargs: {"quality": "same"},
                data_sufficiency=DataSufficiency.DATA_INSUFFICIENT,
            ),
            "risk-1",
            SimpleNamespace(
                model_dump=lambda **kwargs: {"risk": "same"},
                health_escalation_allowed=True,
            ),
        )

    def _fail_request_compatibility(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


def test_urgent_or_unusable_terminal_write_follows_complete_final_gate() -> None:
    events: list[str] = []
    repository = _FinalGateRepository(events)
    source = SimpleNamespace(
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        operation_json={
            "night_episode_revision_id": "revision-1",
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        },
        facts=SimpleNamespace(
            deterministic_quality={"quality": "same"},
            deterministic_risk={"risk": "same"},
        ),
    )

    repository.complete_report_request(
        _recovery_lease(),
        source=cast(Any, source),
        state="urgent_handled",
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert events == ["epochs", "operation", "gate", "terminal_update"]


class _StaleFinalGateRepository(_FinalGateRepository):
    def _lock_current_report_gate(self, *args: Any, **kwargs: Any):
        del args, kwargs
        self.events.append("gate_changed")
        return (
            "revision-2",
            "quality-2",
            SimpleNamespace(),
            "risk-2",
            SimpleNamespace(),
        )

    def _reroute_changed_report_gate(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.events.append("reroute")


@pytest.mark.parametrize("state", ("urgent_handled", "unusable_blocked"))
def test_stale_report_gate_is_rerouted_without_terminal_write(
    state: str,
) -> None:
    events: list[str] = []
    repository = _StaleFinalGateRepository(events)
    source = SimpleNamespace(
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        operation_json={
            "night_episode_revision_id": "revision-1",
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        },
        facts=SimpleNamespace(
            deterministic_quality={},
            deterministic_risk={},
        ),
    )

    closed = repository.complete_report_request(
        _recovery_lease(),
        source=cast(Any, source),
        state=cast(Any, state),
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert closed is False
    assert events == ["epochs", "operation", "gate_changed", "reroute"]


def test_rerouted_report_gate_returns_pending_without_shared_work() -> None:
    processor = _GateProcessor(urgent=True, sufficiency="sufficient")
    processor.repository.complete_report_request = (  # type: ignore[method-assign]
        lambda *args, **kwargs: False
    )

    result = processor.route_report_request(
        _recovery_scope(),
        _recovery_lease(),
    )

    assert result.state == "pending"
    assert processor.repository.shared_reservations == 0


def test_changed_report_gate_refreshes_pins_and_releases_request_to_retry() -> None:
    cursor = _RepairCursor()
    repository = PostgresProductAgentRepository(
        cast(Any, object()),
        _recovery_scope(),
    )

    repository._reroute_changed_report_gate(
        cast(Any, cursor),
        _recovery_lease(),
        operation_json={
            "schema_version": "backend_operation.v2",
            "night_episode_revision_id": "revision-1",
            "quality_assessment_id": "quality-1",
            "current_risk_id": "risk-1",
        },
        current_revision_id="revision-2",
        current_quality_id="quality-2",
        current_risk_id="risk-2",
        rerouted_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    sql, params = cursor.calls[0]
    refreshed = json.loads(params[0])
    assert "SET status = 'retry'" in sql
    assert refreshed["night_episode_revision_id"] == "revision-2"
    assert refreshed["quality_assessment_id"] == "quality-2"
    assert refreshed["current_risk_id"] == "risk-2"
    assert refreshed["gate_reroute_count"] == 1
