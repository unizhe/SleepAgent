from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.radar_agent.persistence.migrations import (
    MIGRATION_VERSION,
    RADAR_AGENT_POSTGRES_MIGRATIONS,
)
from sleepagent.radar_agent.product_agent.contracts import (
    CommunicationDraft,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
)
from sleepagent.radar_agent.product_agent.agents import ProductAgentFactory
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ProductEpisodeRunResult,
)
from sleepagent.sleep_domain import (
    AgentAnalysisTrigger,
    AlgorithmVersionValue,
    AnalysisRole,
    AnalysisStatus,
    AvailabilityState,
    CalibrationValue,
    CollectionWindowDerivation,
    ConfidenceValue,
    DataMode,
    DataSufficiency,
    DeviceBindingReference,
    DomainNamespace,
    NightEpisode,
    NightEpisodeAgentBridge,
    NightEpisodeRevision,
    NightEpisodeState,
    NightRevisionCause,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    Operation,
    OperationStatus,
    PersistentProductDataProvider,
    ProductDataAuthorization,
    ProductRevisionFacts,
    RoleViewAuthorization,
    RoleViewStatus,
    SourceKind,
    SleepDomainRepository,
    SleepObservation,
    TimezoneStatus,
    VendorAlertPayload,
    assert_agent_safe_payload,
)
from sleepagent.sleep_domain.crypto import RawPayloadEncryptionPolicy
from sleepagent.sleep_domain.product_data import (
    INTERNAL_AGENT_ANALYSIS_SCOPE,
    ROLE_VIEW_SCOPES,
)
from sleepagent.sleep_domain.repository import ObservationConflictRecord


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
SHA = "a" * 64
LIVE = DomainNamespace("live:agent-bridge-test", DataMode.LIVE)
REPLAY = DomainNamespace("replay:agent-bridge-test", DataMode.REPLAY)


def _repository() -> SleepDomainRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SleepDomainRepository(
        RadarPersistenceStore.connect_sqlite(connection),
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="agent-bridge-test-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=1),
            production=False,
        ),
    )


def _episode(
    namespace: DomainNamespace,
    *,
    episode_id: str = "night-1",
    subject_id: str = "subject-1",
) -> NightEpisode:
    return NightEpisode(
        night_episode_id=episode_id,
        data_mode=namespace.data_mode,
        subject_id=subject_id,
        timezone_name="Asia/Shanghai",
        local_sleep_date=date(2026, 7, 30),
        night_key=f"{subject_id}:2026-07-30",
        collection_start_at=NOW - timedelta(hours=9),
        collection_end_at=NOW - timedelta(hours=1),
        collection_window_derivation=CollectionWindowDerivation.EXTERNAL_COMMAND,
        binding_references=(
            DeviceBindingReference(
                device_binding_id=f"binding:{episode_id}",
                binding_version=1,
                device_id=f"device:{episode_id}",
            ),
        ),
        data_sufficiency=DataSufficiency.SUFFICIENT,
        pinned_adapter_versions={"perceptor": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={
            "quality": "quality.v1",
            "risk": "risk.v1",
            "night-boundary": "night.v1",
        },
        state=NightEpisodeState.ANALYZED,
        created_at=NOW - timedelta(hours=9),
        updated_at=NOW,
    )


def _revision(
    namespace: DomainNamespace,
    *,
    episode_id: str = "night-1",
    revision_id: str = "night-revision-1",
    subject_id: str = "subject-1",
    observation_ids: tuple[str, ...] = (),
    sufficiency: DataSufficiency = DataSufficiency.SUFFICIENT,
) -> NightEpisodeRevision:
    return NightEpisodeRevision(
        night_episode_revision_id=revision_id,
        night_episode_id=episode_id,
        data_mode=namespace.data_mode,
        subject_id=subject_id,
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=observation_ids,
        observation_set_sha256=SHA,
        data_sufficiency=sufficiency,
        created_at=NOW,
    )


def _persist_revision(
    repository: SleepDomainRepository,
    namespace: DomainNamespace,
    *,
    episode_id: str = "night-1",
    revision_id: str = "night-revision-1",
    subject_id: str = "subject-1",
) -> NightEpisodeRevision:
    repository.create_night_episode(
        namespace,
        _episode(
            namespace,
            episode_id=episode_id,
            subject_id=subject_id,
        ),
    )
    revision = _revision(
        namespace,
        episode_id=episode_id,
        revision_id=revision_id,
        subject_id=subject_id,
    )
    repository.append_night_episode_revision(namespace, revision)
    return revision


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(
            state=AvailabilityState.NOT_PROVIDED,
            reason="not_provided",
        ),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED,
            reason="not_provided",
        ),
        calibration=CalibrationValue(
            state=AvailabilityState.NOT_PROVIDED,
        ),
        quality_flags=("vendor_derived",),
        processing_steps=("perceptor_normalization.v1",),
        limitations=("not_a_diagnosis",),
    )


def _vendor_alert_observation(
    *,
    data_mode: DataMode = DataMode.LIVE,
) -> SleepObservation:
    return SleepObservation(
        observation_id="observation-alert-1",
        data_mode=data_mode,
        observation_type=ObservationType.VENDOR_ALERT,
        payload=VendorAlertPayload(
            alert_code="opaque-code-7",
            title="SECRET VENDOR TITLE",
            message="SECRET VENDOR MESSAGE",
        ),
        subject_id="subject-1",
        device_id="device-1",
        device_binding_id="binding-1",
        binding_version=1,
        event_occurred_at=NOW - timedelta(hours=2),
        received_at=NOW - timedelta(hours=2),
        source_timestamp_text="SECRET VENDOR TIMESTAMP TEXT",
        timezone_status=TimezoneStatus.KNOWN,
        source_kind=SourceKind.VENDOR_DERIVED,
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="perceptor",
            provider_account_id="account-1",
            adapter_id="perceptor",
            adapter_version="1.0.0",
            raw_ingress_record_id="raw-secret-1",
            raw_payload_sha256=SHA,
            acquisition_receipt_ids=("receipt-1",),
        ),
        source_key="source-secret",
        idempotency_key="idempotency-secret",
    )


class _ProviderRepository:
    def __init__(self, observation: SleepObservation) -> None:
        self.observation = observation
        self.episode = _episode(LIVE)
        self.revision = _revision(
            LIVE,
            observation_ids=(observation.observation_id,),
        )

    def get_night_episode_revision(self, _namespace, *, night_episode_revision_id):
        assert night_episode_revision_id == self.revision.night_episode_revision_id
        return self.revision

    def get_night_episode(self, _namespace, *, night_episode_id):
        assert night_episode_id == self.episode.night_episode_id
        return self.episode

    def get_observation(self, _namespace, *, observation_id):
        assert observation_id == self.observation.observation_id
        return self.observation

    def list_observation_conflicts(self, _namespace, *, observation_ids):
        assert observation_ids == (self.observation.observation_id,)
        return (
            ObservationConflictRecord(
                conflict_id="conflict-1",
                fact_slot_key="private-slot-key-is-not-projected",
                first_observation_id=self.observation.observation_id,
                second_observation_id=self.observation.observation_id,
                detected_at=NOW,
            ),
        )

    def get_current_quality(self, _namespace, *, night_episode_id):
        return None

    def get_current_risk(self, _namespace, *, night_episode_id):
        return None


def test_persistent_product_provider_strips_vendor_text_and_exposes_conflict_refs() -> None:
    provider = PersistentProductDataProvider(
        _ProviderRepository(_vendor_alert_observation())  # type: ignore[arg-type]
    )
    facts = provider.load_revision(
        LIVE,
        night_episode_revision_id="night-revision-1",
        authorization=ProductDataAuthorization(
            actor_id="internal-worker",
            subject_id="subject-1",
            role="system",
            data_mode=DataMode.LIVE,
            authorization_scope=(INTERNAL_AGENT_ANALYSIS_SCOPE,),
        ),
    )

    prompt_material = json.dumps(
        {
            "facts": facts.model_dump(mode="json"),
            "tool_inputs": facts.tool_inputs(),
        },
        ensure_ascii=False,
    )
    assert "SECRET VENDOR" not in prompt_material
    assert '"raw_payload"' not in prompt_material
    assert '"data_payload"' not in prompt_material
    assert '"source_timestamp_text"' not in prompt_material
    assert "raw_ingress:live:raw-secret-1:sha256:" in prompt_material
    assert facts.conflict_summaries[0]["resolution"] == "unresolved"
    assert (
        facts.canonical_observations[0]["payload"]["severity"]
        == "unknown"
    )

    with pytest.raises(ValueError, match="raw_payload"):
        assert_agent_safe_payload(
            {"data_mode": "live", "raw_payload": {"secret": True}},
            expected_data_mode=DataMode.LIVE,
        )
    with pytest.raises(ValueError, match="data_payload"):
        assert_agent_safe_payload(
            {"data_mode": "live", "data_payload": "secret"},
            expected_data_mode=DataMode.LIVE,
        )


def test_agent_bridge_migration_is_additive_and_has_no_external_api() -> None:
    assert MIGRATION_VERSION == "020_legacy_authority_cutover"
    sql = RADAR_AGENT_POSTGRES_MIGRATIONS["017_product_agent_bridge"]
    assert "sleep_domain_analysis_role_views" in sql
    assert "analysis_run_id" in sql
    assert "idx_sleep_domain_agent_operation_fairness" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_persistent_product_provider_rejects_cross_mode_observation() -> None:
    provider = PersistentProductDataProvider(
        _ProviderRepository(  # type: ignore[arg-type]
            _vendor_alert_observation(data_mode=DataMode.REPLAY)
        )
    )
    with pytest.raises(Exception, match="cross-subject or cross-mode"):
        provider.load_revision(
            LIVE,
            night_episode_revision_id="night-revision-1",
            authorization=ProductDataAuthorization(
                actor_id="internal-worker",
                subject_id="subject-1",
                role="system",
                data_mode=DataMode.LIVE,
                authorization_scope=(INTERNAL_AGENT_ANALYSIS_SCOPE,),
            ),
        )


class _VersionStore:
    def get(self, _subject_id: str):
        return SimpleNamespace(version=0)


class _ConfiguredModel:
    def __init__(self, configured: bool) -> None:
        self.is_configured = configured


class _RecordingRunner:
    def __init__(self, *, configured: bool = True) -> None:
        model = _ConfiguredModel(configured)
        self.agent_roster = ProductAgentFactory.create(
            sleepcare_model=model,
            evidence_reasoning_model=model,
            care_strategy_model=model,
            safety_review_model=model,
        )
        self.commit_controller = SimpleNamespace(
            care_store=_VersionStore(),
            memory_store=_VersionStore(),
        )
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        insufficient = request.episode_type == EpisodeType.DATA_QUALITY_RECOVERY
        mode = (
            ExecutionMode.DETERMINISTIC_ONLY
            if insufficient
            else ExecutionMode.INTELLIGENT
        )
        status = EpisodeStatus.PARTIAL if insufficient else EpisodeStatus.COMPLETE
        publication = CommunicationDraft(
            draft_id=f"draft:{request.episode_id}",
            audience_role=request.audience_role,
            text=(
                "当前数据不足，无法形成可靠判断。"
                if insufficient
                else f"{request.audience_role} view for exact revision"
            ),
            claim_refs=["canonical-claim:1"] if not insufficient else [],
            context_notice=(
                "明确的数据不足结果。"
                if insufficient
                else "来自同一精确 revision。"
            ),
        )
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=1,
            terminal=True,
            execution_mode=mode,
            status=status,
            goal_achieved=not insufficient,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=0,
            failure_codes=["insufficient_data_quality"] if insufficient else [],
            trace_ref=f"trace:{request.episode_id}",
        )
        return ProductEpisodeRunResult(
            registry_hash=SHA,
            receipt=receipt,
            publication=publication,
            publication_delivered=True,
        )


class _StaticFactsProvider(PersistentProductDataProvider):
    def load_revision(
        self,
        namespace,
        *,
        night_episode_revision_id,
        authorization,
    ):
        revision = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=night_episode_revision_id,
        )
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=revision.night_episode_id,
        )
        return ProductRevisionFacts(
            night_episode_id=revision.night_episode_id,
            night_episode_revision_id=revision.night_episode_revision_id,
            night_episode_revision_number=revision.revision_number,
            subject_id=revision.subject_id,
            data_mode=namespace.data_mode,
            timezone_name=episode.timezone_name,
            local_sleep_date=episode.local_sleep_date.isoformat(),
            data_sufficiency=revision.data_sufficiency.value,
            canonical_observations=(
                {
                    "observation_ref": "canonical_observation:1",
                    "data_mode": namespace.data_mode.value,
                    "observation_type": "heart_rate",
                    "payload": {
                        "observation_type": "heart_rate",
                        "value": 65,
                        "unit": "beats_per_minute",
                    },
                    "provenance_references": [
                        f"raw_ingress:{namespace.data_mode.value}:raw-1:sha256:{SHA}"
                    ],
                },
            ),
            deterministic_quality={
                "data_mode": namespace.data_mode.value,
                "data_sufficiency": revision.data_sufficiency.value,
                "coverage_ratio": (
                    1.0
                    if revision.data_sufficiency == DataSufficiency.SUFFICIENT
                    else 0.0
                ),
                "reason_codes": [],
            },
            deterministic_risk={
                "data_mode": namespace.data_mode.value,
                "risk_state": "unknown",
                "data_sufficiency": revision.data_sufficiency.value,
                "is_all_clear": False,
                "reason_codes": ["no_reviewed_signal"],
            },
            conflict_summaries=(),
            provenance_references=(
                f"night_episode_revision:{namespace.data_mode.value}:{night_episode_revision_id}:{SHA}",
            ),
            canonical_data_version=SHA,
        )


def _bridge(
    repository: SleepDomainRepository,
    runner: _RecordingRunner,
) -> NightEpisodeAgentBridge:
    return NightEpisodeAgentBridge(
        repository=repository,
        data_provider=_StaticFactsProvider(repository),
        episode_runner=runner,  # type: ignore[arg-type]
        lease_duration=timedelta(minutes=5),
    )


def _submit_and_process(
    bridge: NightEpisodeAgentBridge,
    namespace: DomainNamespace,
    *,
    revision_id: str,
    trigger: AgentAnalysisTrigger,
    suffix: str,
    at: datetime,
):
    operation, created = bridge.submit_analysis(
        namespace,
        night_episode_revision_id=revision_id,
        trigger=trigger,
        service_principal_id="internal-scheduler",
        actor_id="authorized-internal-actor",
        idempotency_key=f"idempotency:{suffix}",
        correlation_id=f"correlation:{suffix}",
        submitted_at=at,
    )
    assert created
    leased = bridge.lease_next(
        namespace,
        worker_id=f"worker:{suffix}",
        now=at + timedelta(seconds=1),
    )
    assert leased is not None
    analysis = bridge.process_leased(
        namespace,
        leased,
        worker_id=f"worker:{suffix}",
        completed_at=at + timedelta(seconds=2),
    )
    return operation, analysis


def test_bridge_appends_exact_versioned_analysis_and_three_authorized_views() -> None:
    repository = _repository()
    revision = _persist_revision(repository, LIVE)
    runner = _RecordingRunner()
    bridge = _bridge(repository, runner)

    operation, first = _submit_and_process(
        bridge,
        LIVE,
        revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.MORNING_ANALYSIS,
        suffix="morning",
        at=NOW + timedelta(minutes=1),
    )
    assert first.status == AnalysisStatus.READY
    assert first.night_episode_revision_id == revision.night_episode_revision_id
    assert first.night_episode_revision_number == revision.revision_number
    views = repository.list_analysis_role_views(
        LIVE,
        analysis_revision_id=first.analysis_revision_id,
    )
    assert {view.role for view in views} == set(AnalysisRole)
    assert {view.night_episode_revision_id for view in views} == {
        revision.night_episode_revision_id
    }
    assert {view.status for view in views} == {RoleViewStatus.READY}
    assert first.analysis_run_id == next(
        view.product_agent_episode_id
        for view in views
        if view.role == AnalysisRole.ELDER
    )
    assert repository.get_operation(
        LIVE,
        operation_id=operation.operation_id,
    ).status == OperationStatus.SUCCEEDED
    assert len(runner.requests) == 3
    assert {request.audience_role for request in runner.requests} == {
        "elder",
        "family",
        "doctor",
    }
    assert all(
        request.fact_snapshot.binding.subject_id.startswith(
            "live:agent-bridge-test::"
        )
        for request in runner.requests
    )
    assert all(
        len(request.runtime_readiness_decisions) == 7
        and request.fact_snapshot.readiness_decision_refs
        for request in runner.requests
    )
    prompt_material = json.dumps(
        [request.tool_inputs for request in runner.requests],
        ensure_ascii=False,
    )
    assert '"raw_payload"' not in prompt_material
    assert '"data_payload"' not in prompt_material
    assert "replay:" not in prompt_material

    _, second = _submit_and_process(
        bridge,
        LIVE,
        revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.EXPLICIT_REANALYSIS,
        suffix="reanalysis",
        at=NOW + timedelta(minutes=2),
    )
    assert second.revision_number == 2
    assert second.parent_analysis_revision_id == first.analysis_revision_id
    assert second.night_episode_revision_id == first.night_episode_revision_id
    assert second.analysis_run_id != first.analysis_run_id


def test_model_unavailable_is_degraded_and_never_claimed_ready() -> None:
    repository = _repository()
    revision = _persist_revision(repository, LIVE)
    runner = _RecordingRunner(configured=False)
    bridge = _bridge(repository, runner)

    operation, analysis = _submit_and_process(
        bridge,
        LIVE,
        revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.INTERNAL_ANALYSIS,
        suffix="model-outage",
        at=NOW + timedelta(minutes=1),
    )

    assert runner.requests == []
    assert analysis.status == AnalysisStatus.DEGRADED
    assert analysis.execution_mode == "safe_degraded"
    assert analysis.failure_codes == ("MODEL_UNAVAILABLE",)
    assert analysis.model_versions == {}
    assert {
        view.status
        for view in repository.list_analysis_role_views(
            LIVE,
            analysis_revision_id=analysis.analysis_revision_id,
        )
    } == {RoleViewStatus.DEGRADED}
    stored_operation = repository.get_operation(
        LIVE,
        operation_id=operation.operation_id,
    )
    assert stored_operation.status == OperationStatus.FAILED
    assert stored_operation.error_code == "MODEL_UNAVAILABLE"


def test_data_insufficient_uses_deterministic_degraded_views() -> None:
    repository = _repository()
    episode = _episode(LIVE)
    repository.create_night_episode(LIVE, episode)
    revision = _revision(
        LIVE,
        sufficiency=DataSufficiency.DATA_INSUFFICIENT,
    )
    repository.append_night_episode_revision(LIVE, revision)
    runner = _RecordingRunner(configured=False)
    bridge = _bridge(repository, runner)

    _, analysis = _submit_and_process(
        bridge,
        LIVE,
        revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.MORNING_ANALYSIS,
        suffix="insufficient",
        at=NOW + timedelta(minutes=1),
    )

    assert len(runner.requests) == 3
    assert {
        request.episode_type for request in runner.requests
    } == {EpisodeType.DATA_QUALITY_RECOVERY}
    assert analysis.status == AnalysisStatus.DEGRADED
    assert analysis.execution_mode == "deterministic_only"
    assert "DATA_INSUFFICIENT" in analysis.failure_codes


def test_role_view_queries_are_committed_only_and_role_scoped() -> None:
    repository = _repository()
    revision = _persist_revision(repository, LIVE)
    runner = _RecordingRunner()
    bridge = _bridge(repository, runner)
    _, analysis = _submit_and_process(
        bridge,
        LIVE,
        revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.FEEDBACK,
        suffix="feedback",
        at=NOW + timedelta(minutes=1),
    )
    calls_before_query = len(runner.requests)
    provider = PersistentProductDataProvider(repository)
    family = provider.get_role_view(
        LIVE,
        analysis_revision_id=analysis.analysis_revision_id,
        authorization=RoleViewAuthorization(
            actor_id="family-actor",
            subject_id=revision.subject_id,
            role=AnalysisRole.FAMILY,
            data_mode=DataMode.LIVE,
            authorization_scope=(ROLE_VIEW_SCOPES[AnalysisRole.FAMILY],),
        ),
    )
    assert family.role == AnalysisRole.FAMILY
    assert len(runner.requests) == calls_before_query

    with pytest.raises(PermissionError, match="scope"):
        provider.get_role_view(
            LIVE,
            analysis_revision_id=analysis.analysis_revision_id,
            authorization=RoleViewAuthorization(
                actor_id="elder-actor",
                subject_id=revision.subject_id,
                role=AnalysisRole.ELDER,
                data_mode=DataMode.LIVE,
                authorization_scope=(ROLE_VIEW_SCOPES[AnalysisRole.DOCTOR],),
            ),
        )
    with pytest.raises(PermissionError, match="outside authorization"):
        provider.get_role_view(
            LIVE,
            analysis_revision_id=analysis.analysis_revision_id,
            authorization=RoleViewAuthorization(
                actor_id="doctor-actor",
                subject_id="different-subject",
                role=AnalysisRole.DOCTOR,
                data_mode=DataMode.LIVE,
                authorization_scope=(ROLE_VIEW_SCOPES[AnalysisRole.DOCTOR],),
            ),
        )


def test_live_and_replay_runs_use_distinct_internal_subject_namespaces() -> None:
    repository = _repository()
    live_revision = _persist_revision(
        repository,
        LIVE,
        episode_id="live-night",
        revision_id="live-revision",
    )
    replay_revision = _persist_revision(
        repository,
        REPLAY,
        episode_id="replay-night",
        revision_id="replay-revision",
    )
    runner = _RecordingRunner()
    bridge = _bridge(repository, runner)
    _submit_and_process(
        bridge,
        LIVE,
        revision_id=live_revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.INTERNAL_ANALYSIS,
        suffix="live",
        at=NOW + timedelta(minutes=1),
    )
    _submit_and_process(
        bridge,
        REPLAY,
        revision_id=replay_revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.INTERNAL_ANALYSIS,
        suffix="replay",
        at=NOW + timedelta(minutes=2),
    )
    internal_subjects = {
        request.fact_snapshot.binding.subject_id for request in runner.requests
    }
    assert any(subject.startswith("live:") for subject in internal_subjects)
    assert any(subject.startswith("replay:") for subject in internal_subjects)
    assert len(internal_subjects) == 2


def test_only_explicit_slow_path_triggers_can_submit_and_submission_is_fast() -> None:
    repository = _repository()
    revision = _persist_revision(repository, LIVE)
    runner = _RecordingRunner()
    bridge = _bridge(repository, runner)

    with pytest.raises(ValueError, match="unsupported"):
        bridge.submit_analysis(
            LIVE,
            night_episode_revision_id=revision.night_episode_revision_id,
            trigger="per_sample",  # type: ignore[arg-type]
            service_principal_id="webhook",
            actor_id="webhook",
            idempotency_key="sample-1",
            correlation_id="sample-1",
            submitted_at=NOW,
        )
    operation, created = bridge.submit_analysis(
        LIVE,
        night_episode_revision_id=revision.night_episode_revision_id,
        trigger=AgentAnalysisTrigger.FOLLOW_UP,
        service_principal_id="internal",
        actor_id="actor",
        idempotency_key="follow-up-1",
        correlation_id="follow-up-1",
        submitted_at=NOW,
    )
    assert created
    assert operation.status == OperationStatus.PENDING
    assert runner.requests == []


def test_persistent_operation_leasing_preserves_subject_order_and_fairness() -> None:
    repository = _repository()

    def operation(
        operation_id: str,
        subject_id: str,
        created_at: datetime,
    ) -> Operation:
        return Operation(
            operation_id=operation_id,
            data_mode=DataMode.LIVE,
            operation_type=(
                "product_agent_analysis:explicit_reanalysis"
            ),
            subject_id=subject_id,
            service_principal_id="internal",
            actor_id="actor",
            target_resource_id=f"revision:{operation_id}",
            idempotency_key=operation_id,
            request_sha256=SHA,
            status=OperationStatus.PENDING,
            correlation_id=operation_id,
            created_at=created_at,
            updated_at=created_at,
        )

    repository.create_operation(
        LIVE,
        operation("a-1", "subject-a", NOW),
    )
    repository.create_operation(
        LIVE,
        operation("a-2", "subject-a", NOW + timedelta(seconds=1)),
    )
    repository.create_operation(
        LIVE,
        operation("b-1", "subject-b", NOW + timedelta(seconds=2)),
    )
    first = repository.lease_next_operation(
        LIVE,
        operation_type_prefix="product_agent_analysis:",
        worker_id="worker-1",
        now=NOW + timedelta(seconds=3),
        lease_duration=timedelta(minutes=1),
    )
    second = repository.lease_next_operation(
        LIVE,
        operation_type_prefix="product_agent_analysis:",
        worker_id="worker-2",
        now=NOW + timedelta(seconds=3),
        lease_duration=timedelta(minutes=1),
    )
    assert first.operation.operation_id == "a-1"
    assert second.operation.operation_id == "b-1"
