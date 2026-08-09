from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.radar_agent.product_agent.acceptance import (
    state_persistence_receipt_from_restart,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    InvocationOutcome,
    MemoryChangeCandidate,
    SourceScope,
    SourceScopeKind,
    ToolReceipt,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.external_actions import (
    ConfiguredExternalActionExecutor,
    ExternalActionConfigurationError,
    ExternalActionExecutionRequest,
    ExternalActionExecutionResult,
    UnconfiguredExternalActionExecutor,
)
from sleepagent.radar_agent.product_agent.governance import (
    CareContextState,
    CareTransitionEvent,
    CommitJournalEntry,
    DeterministicCommitController,
)
from sleepagent.radar_agent.product_agent.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    HumanDecisionChoice,
    HumanDecisionService,
    HumanDecisionStatus,
    VerifiedApprovalCapability,
)
from sleepagent.radar_agent.product_agent.product_persistence import (
    PersistentCareContextStore,
    PersistentCommitJournal,
    PersistentMemoryContextStore,
    PersistentProductEpisodeResultStore,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ProductEpisodeRunResult,
)
from sleepagent.radar_agent.product_agent.runtime_factory import (
    build_product_episode_runner_from_env,
)


NOW = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)


def _persistence(path: Path) -> RadarPersistenceStore:
    return RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )


def _snapshot() -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="snapshot:persistence",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="UTC",
            date_start=date(2026, 7, 27),
            date_end=date(2026, 7, 27),
            valid_night_count=1,
        ),
        canonical_data_version="canonical-v1",
        created_at=NOW,
    )


def _approved_capability(
    *,
    action_kind: str,
    action_scope: str,
    target_id: str,
    target_hash: str,
    idempotency_key: str,
    fact_snapshot: FactSnapshot,
) -> tuple[HumanDecisionService, VerifiedApprovalCapability]:
    service = HumanDecisionService()
    request = service.create(
        ActionProposal(
            proposal_id=f"proposal:persistence:{action_scope}:{target_id}",
            episode_id="episode:persistence",
            subject_id=fact_snapshot.binding.subject_id,
            proposer_actor_id=fact_snapshot.binding.actor_id,
            action_kind=action_kind,
            action_scope=action_scope,
            target_id=target_id,
            target_hash=target_hash,
            fact_snapshot_id=fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
            policy_version=HITL_POLICY_VERSION,
            payload={"target_id": target_id},
            explanation=DecisionExplanation(
                what_will_change=f"Execute {action_scope}",
                why_now="The exact persisted target is ready for execution.",
                who_will_receive_or_be_affected=fact_snapshot.binding.subject_id,
                duration_or_frequency="One bounded execution.",
                how_to_revoke="Revoke before execution begins.",
            ),
            created_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )
    )
    approved = service.decide(
        request.decision_id,
        actor_id=fact_snapshot.binding.actor_id,
        actor_role=fact_snapshot.binding.role,
        choice=HumanDecisionChoice.APPROVE,
        target_hash=target_hash,
        now=NOW + timedelta(minutes=1),
    )
    assert approved.status == HumanDecisionStatus.APPROVED
    capability = service.acquire_verified_capability(
        request.decision_id,
        expected_proposal_id=request.proposal.proposal_id,
        expected_subject_id=request.proposal.subject_id,
        expected_target_id=request.proposal.target_id,
        expected_target_hash=request.proposal.target_hash,
        expected_action_scope=request.proposal.action_scope,
        expected_fact_snapshot_id=fact_snapshot.fact_snapshot_id,
        expected_fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
        expected_policy_version=HITL_POLICY_VERSION,
        idempotency_key=idempotency_key,
        now=NOW + timedelta(minutes=2),
    )
    return service, capability


def _assert_authority_refs(
    receipt: ToolReceipt,
    capability: VerifiedApprovalCapability,
) -> None:
    grant = capability.grant
    assert receipt.source_refs == [
        f"human-decision:{grant.decision_id}",
        f"approval-grant:{grant.grant_id}:{grant.grant_hash}",
    ]


def _controller(persistence: RadarPersistenceStore) -> DeterministicCommitController:
    return DeterministicCommitController(
        care_store=PersistentCareContextStore(persistence),
        memory_store=PersistentMemoryContextStore(persistence),
        commit_journal=PersistentCommitJournal(persistence),
    )


def test_memory_care_and_commit_receipts_survive_process_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "product-state.sqlite3"
    first_persistence = _persistence(database)
    first = _controller(first_persistence)
    snap = _snapshot()
    memory = MemoryChangeCandidate(
        candidate_id="memory-persistent-1",
        operation="create",
        subject_id="subject-1",
        memory_type="routine",
        concept_id="sleep.preferred_wake_time",
        value_schema_id="bounded_string.v1",
        typed_value="希望每天七点起床",
        provenance_type="elder_confirmed",
        source_ref="user_report:persistent-1",
        sensitivity_class="personal",
        allowed_roles=(AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING),
        allowed_purposes=(
            "personal_evidence_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ),
        explicit_user_authorization=True,
        confirmation_required=False,
    )
    decision_service, capability = _approved_capability(
        action_kind="memory",
        action_scope="commit_memory",
        target_id=memory.candidate_id,
        target_hash=memory.candidate_hash,
        idempotency_key="memory:persistent:1",
        fact_snapshot=snap,
    )
    first_receipt = first.commit_memory(
        candidate=memory,
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="memory:persistent:1",
        approval_capability=capability,
    )
    first.care_store.compare_and_set(
        "subject-1",
        0,
        CareContextState(
            subject_id="subject-1",
            version=1,
            active_primary_action={"candidate_id": "care-persistent-1"},
            transition_history=[
                CareTransitionEvent(
                    strategy_id="activation:care-persistent-1",
                    disposition="propose",
                    to_candidate_id="care-persistent-1",
                    committed_at=NOW,
                )
            ],
        ),
    )
    first_persistence.connection.close()

    restarted_persistence = _persistence(database)
    restarted = _controller(restarted_persistence)
    memory_state = restarted.memory_store.get("subject-1")
    care_state = restarted.care_store.get("subject-1")
    replay = restarted.commit_memory(
        candidate=memory,
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="memory:persistent:1",
        approval_capability=capability,
    )

    assert memory_state.version == 1
    assert memory_state.items[0].value == "希望每天七点起床"
    assert care_state.version == 1
    assert care_state.active_primary_action == {
        "candidate_id": "care-persistent-1"
    }
    assert care_state.transition_history[0].disposition == "propose"
    assert replay == first_receipt
    _assert_authority_refs(first_receipt, capability)
    _assert_authority_refs(replay, capability)
    decision = decision_service.record_execution_result(
        capability,
        status=HumanDecisionStatus.COMMITTED,
        receipt_ref=first_receipt.tool_invocation_id,
        now=NOW + timedelta(minutes=3),
    )
    assert decision.status == HumanDecisionStatus.COMMITTED
    assert restarted.memory_store.get("subject-1").version == 1
    proof = state_persistence_receipt_from_restart(
        state_kind="memory",
        subject_id="subject-1",
        version_before=0,
        version_after_restart=memory_state.version,
        commit_receipt=replay,
        restarted_at=NOW + timedelta(hours=1),
    )
    assert proof.restart_verified
    assert proof.subject_ref_hash == stable_hash("subject-1")


def test_production_runner_factory_uses_database_backed_product_state(
    tmp_path: Path,
) -> None:
    persistence = _persistence(tmp_path / "factory-state.sqlite3")

    def resolver(_refs, _query):
        return None

    runner = build_product_episode_runner_from_env(
        persistence_store=persistence,
        source_resolvers={"accepted_ledger": resolver},
    )

    assert isinstance(
        runner.commit_controller.care_store,
        PersistentCareContextStore,
    )
    assert isinstance(
        runner.commit_controller.memory_store,
        PersistentMemoryContextStore,
    )
    assert isinstance(
        runner.commit_controller.commit_journal,
        PersistentCommitJournal,
    )
    assert isinstance(
        runner.result_store,
        PersistentProductEpisodeResultStore,
    )
    assert isinstance(runner.external_executor, ConfiguredExternalActionExecutor)
    assert (
        runner.longitudinal_memory.source_resolvers["accepted_ledger"]
        is resolver
    )
    assert (
        runner.tool_executor.handlers["memory.read"]
        == runner.longitudinal_memory.read
    )
    assert runner.result_store.digest_read_enabled() is False


def test_external_effect_receipt_is_durable_and_not_reexecuted_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "external-journal.sqlite3"
    calls: list[ExternalActionExecutionRequest] = []

    def execute(
        request: ExternalActionExecutionRequest,
    ) -> ExternalActionExecutionResult:
        calls.append(request)
        return ExternalActionExecutionResult(
            provider="test-gateway",
            provider_request_id="gateway-request-1",
            delivery_status="pending",
            executed_at=NOW,
        )

    target_hash = "e" * 64
    snap = _snapshot()
    decision_service, capability = _approved_capability(
        action_kind="external_action",
        action_scope="share_artifact",
        target_id="share-target-1",
        target_hash=target_hash,
        idempotency_key="external:persistent:1",
        fact_snapshot=snap,
    )
    first_persistence = _persistence(database)
    first = _controller(first_persistence)
    receipt = first.execute_external(
        tool_name="external.share",
        target={"artifact_ref": "artifact-1", "recipient": "doctor-1"},
        snapshot=snap,
        idempotency_key="external:persistent:1",
        executor=execute,
        approval_capability=capability,
        actor_id="actor-1",
        subject_id="subject-1",
        action_scope="share_artifact",
        target_id="share-target-1",
        target_version=1,
        target_hash=target_hash,
    )
    first_persistence.connection.close()

    restarted = _controller(_persistence(database))
    replay = restarted.execute_external(
        tool_name="external.share",
        target={"artifact_ref": "artifact-1", "recipient": "doctor-1"},
        snapshot=snap,
        idempotency_key="external:persistent:1",
        executor=execute,
        approval_capability=capability,
        actor_id="actor-1",
        subject_id="subject-1",
        action_scope="share_artifact",
        target_id="share-target-1",
        target_version=1,
        target_hash=target_hash,
    )

    assert receipt == replay
    assert receipt.outcome == InvocationOutcome.SUCCEEDED
    assert receipt.output["delivery_status"] == "pending"
    assert len(calls) == 1
    _assert_authority_refs(receipt, capability)
    _assert_authority_refs(replay, capability)
    decision = decision_service.record_execution_result(
        capability,
        status=HumanDecisionStatus.COMMITTED,
        receipt_ref=receipt.tool_invocation_id,
        now=NOW + timedelta(minutes=3),
    )
    assert decision.status == HumanDecisionStatus.COMMITTED


def test_pending_commit_reservation_recovers_as_unknown_without_execution(
    tmp_path: Path,
) -> None:
    persistence = _persistence(tmp_path / "pending-journal.sqlite3")
    journal = PersistentCommitJournal(persistence)
    snapshot = _snapshot()
    target_hash = "e" * 64
    target = {"artifact_ref": "artifact-1"}
    decision_service, capability = _approved_capability(
        action_kind="external_action",
        action_scope="share_artifact",
        target_id="share-target-1",
        target_hash=target_hash,
        idempotency_key="external:pending:1",
        fact_snapshot=snapshot,
    )
    grant = capability.grant
    commit_payload = {
        "tool_name": "external.share",
        "target_id": "share-target-1",
        "target_version": 1,
        "target_hash": target_hash,
        "actor_id": "actor-1",
        "subject_id": "subject-1",
        "action_scope": "share_artifact",
        "payload": target,
    }
    authority_refs = (
        f"human-decision:{grant.decision_id}",
        f"approval-grant:{grant.grant_id}:{grant.grant_hash}",
    )
    input_hash = stable_hash(
        {
            "operation": commit_payload,
            "authority": {
                "decision_id": grant.decision_id,
                "proposal_id": grant.proposal_id,
                "grant_id": grant.grant_id,
                "grant_hash": grant.grant_hash,
                "approving_records_hash": grant.approving_records_hash,
            },
        }
    )
    created_at = grant.issued_at
    assert journal.reserve(
        CommitJournalEntry(
            idempotency_key="external:pending:1",
            tool_name="external.share",
            input_hash=input_hash,
            fact_snapshot_hash=snapshot.fact_snapshot_hash,
            authority_refs=authority_refs,
            state="pending",
            created_at=created_at,
            updated_at=created_at,
        )
    )
    entry = journal.get("external:pending:1")

    assert entry is not None
    assert entry.state == "pending"
    assert entry.receipt is None
    calls = 0

    def must_not_execute(
        _request: ExternalActionExecutionRequest,
    ) -> ExternalActionExecutionResult:
        nonlocal calls
        calls += 1
        raise AssertionError("pending external effect must not be retried")

    receipt = DeterministicCommitController(
        commit_journal=journal
    ).execute_external(
        tool_name="external.share",
        target=target,
        snapshot=snapshot,
        idempotency_key="external:pending:1",
        executor=must_not_execute,
        approval_capability=capability,
        actor_id="actor-1",
        subject_id="subject-1",
        action_scope="share_artifact",
        target_id="share-target-1",
        target_version=1,
        target_hash=target_hash,
    )

    assert receipt.outcome == InvocationOutcome.UNKNOWN
    assert receipt.error_code == "IndeterminatePriorAttempt"
    assert calls == 0
    _assert_authority_refs(receipt, capability)
    decision = decision_service.record_execution_result(
        capability,
        status=HumanDecisionStatus.OUTCOME_UNKNOWN,
        receipt_ref=receipt.tool_invocation_id,
        failure_reason=receipt.error_code,
        now=NOW + timedelta(minutes=3),
    )
    assert decision.status == HumanDecisionStatus.OUTCOME_UNKNOWN


def test_product_episode_result_history_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "episode-results.sqlite3"
    persistence = _persistence(database)
    scope = _snapshot().source_scope
    result = ProductEpisodeRunResult(
        registry_hash="b" * 64,
        receipt=EpisodeReceipt(
            episode_id="episode:persistent-result",
            episode_type=EpisodeType.URGENT_BOUNDARY,
            receipt_revision=1,
            terminal=True,
            execution_mode=ExecutionMode.DETERMINISTIC_ONLY,
            status=EpisodeStatus.COMPLETE,
            goal_achieved=True,
            fact_snapshot_id="snapshot:persistence",
            fact_snapshot_hash=_snapshot().fact_snapshot_hash,
            source_scope=scope,
            final_episode_state_revision=1,
            trace_ref="trace:persistent-result",
        ),
        publication_delivered=True,
    )
    PersistentProductEpisodeResultStore(persistence).append_terminal_bundle(
        result,
        subject_id="subject-1",
        now=NOW,
    )
    persistence.connection.close()

    restarted = PersistentProductEpisodeResultStore(_persistence(database))

    assert restarted.latest("episode:persistent-result") == result
    assert restarted.history("episode:persistent-result") == [result]


def test_configured_external_executor_posts_exact_target_and_keeps_receipt(
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "provider": "notification-gateway",
                "request_id": "provider-request-1",
                "status": "accepted",
            },
        )

    executor = ConfiguredExternalActionExecutor(
        endpoints={"external.share": "https://actions.example/share"},
        api_key="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    request = ExternalActionExecutionRequest(
        tool_name="external.share",
        target_id="target-1",
        target_version=2,
        target_hash="c" * 64,
        actor_id="actor-1",
        subject_id="subject-1",
        action_scope="share_artifact",
        fact_snapshot_hash="d" * 64,
        idempotency_key="external:target-1",
        payload={"artifact_ref": "artifact-1"},
    )

    result = executor(request)

    assert result.provider_request_id == "provider-request-1"
    assert result.delivery_status == "pending"
    assert captured["body"] == request.model_dump(mode="json")
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["idempotency-key"] == "external:target-1"
    assert headers["x-sleepagent-target-hash"] == "c" * 64
    assert headers["authorization"] == "Bearer secret-token"


def test_unconfigured_external_executor_fails_closed() -> None:
    with pytest.raises(ExternalActionConfigurationError, match="not configured"):
        UnconfiguredExternalActionExecutor()(
            ExternalActionExecutionRequest(
                tool_name="external.export",
                target_id="target-1",
                target_version=1,
                target_hash="c" * 64,
                actor_id="actor-1",
                subject_id="subject-1",
                action_scope="export_summary",
                fact_snapshot_hash="d" * 64,
                idempotency_key="external:target-1",
                payload={},
            )
        )


def test_external_executor_env_placeholders_stay_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SLEEPAGENT_EXTERNAL_SHARE_URL",
        "<https-share-gateway-url>",
    )
    monkeypatch.setenv(
        "SLEEPAGENT_EXTERNAL_ACTION_TIMEOUT_SECONDS",
        "<seconds>",
    )

    executor = ConfiguredExternalActionExecutor.from_env()

    assert executor.endpoints == {}


def test_external_executor_rejects_cleartext_nonlocal_endpoint() -> None:
    with pytest.raises(
        ExternalActionConfigurationError,
        match="must use HTTPS",
    ):
        ConfiguredExternalActionExecutor(
            endpoints={
                "external.notify": "http://actions.example/notify",
            }
        )
